using System.Security.Cryptography.X509Certificates;
using k8s;
using k8s.Models;

namespace Kuberniq;

/// <summary>
/// Handles all Kubernetes-side work for registering a remote cluster:
///   1. Creates a dedicated ServiceAccount in the target cluster
///   2. Creates a read-only ClusterRole + ClusterRoleBinding
///   3. Issues a permanent token Secret (k8s ≥ 1.24)
///   4. Extracts the server URL + CA certificate
/// Returns the three values the MCP server needs for POST /clusters.
/// </summary>
public static class ClusterRegistrar
{
    private const string ClusterRoleName        = "kuberniq-mcp-reader";
    private const string ClusterRoleBindingName = "kuberniq-mcp-reader";

    public static async Task<(string server, string caData, string token)> SetupAsync(
        string context,
        string saName,
        string saNamespace,
        bool   skipRbac,
        IProgress<string> progress,
        CancellationToken ct = default)
    {
        // ── Connect to the target cluster using the specified kubeconfig context ──
        var kubeconfigPath = Environment.GetEnvironmentVariable("KUBECONFIG")
            ?? Path.Combine(
                   Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                   ".kube", "config");

        var clientCfg = KubernetesClientConfiguration.BuildConfigFromConfigFile(
            new FileInfo(kubeconfigPath), context);

        var k8s = new Kubernetes(clientCfg);

        if (!skipRbac)
        {
            // ── 1. Namespace ────────────────────────────────────────────────────
            progress.Report($"Ensuring namespace '{saNamespace}' exists");
            await EnsureNamespaceAsync(k8s, saNamespace, ct);

            // ── 2. ServiceAccount ───────────────────────────────────────────────
            progress.Report($"Creating ServiceAccount '{saName}'");
            await UpsertServiceAccountAsync(k8s, saName, saNamespace, ct);

            // ── 3. ClusterRole ──────────────────────────────────────────────────
            progress.Report("Applying ClusterRole 'kubeai-mcp-reader'");
            await UpsertClusterRoleAsync(k8s, ct);

            // ── 4. ClusterRoleBinding ───────────────────────────────────────────
            progress.Report("Applying ClusterRoleBinding");
            await UpsertClusterRoleBindingAsync(k8s, saName, saNamespace, ct);
        }

        // ── 5. Permanent token Secret ───────────────────────────────────────────
        // kubectl create token produces a time-limited JWT; an annotated Secret
        // causes the token controller to issue a permanent, auto-rotated token.
        var tokenSecretName = $"{saName}-kuberniq-token";
        progress.Report($"Ensuring token Secret '{tokenSecretName}'");
        await EnsureTokenSecretAsync(k8s, tokenSecretName, saName, saNamespace, ct);

        // ── 6. Wait for the token controller to populate the token ──────────────
        progress.Report("Waiting for token to be issued by the token controller");
        var token = await WaitForTokenAsync(k8s, tokenSecretName, saNamespace, ct);

        // ── 7. Extract server URL + CA cert from the resolved config ────────────
        var server = clientCfg.Host;
        var caData = ExtractCaData(clientCfg);

        return (server, caData, token);
    }

    // ── Helpers ────────────────────────────────────────────────────────────────

    static async Task EnsureNamespaceAsync(Kubernetes k8s, string ns, CancellationToken ct)
    {
        try
        {
            await k8s.CreateNamespaceAsync(
                new V1Namespace { Metadata = new V1ObjectMeta { Name = ns } },
                cancellationToken: ct);
        }
        catch (Exception ex) when (IsAlreadyExists(ex)) { /* already exists */ }
    }

    static async Task UpsertServiceAccountAsync(
        Kubernetes k8s, string name, string ns, CancellationToken ct)
    {
        var sa = new V1ServiceAccount
        {
            Metadata = new V1ObjectMeta
            {
                Name              = name,
                NamespaceProperty = ns,
                Labels            = ManagedByLabel()
            }
        };
        try { await k8s.CreateNamespacedServiceAccountAsync(sa, ns, cancellationToken: ct); }
        catch (Exception ex) when (IsAlreadyExists(ex)) { /* already exists — no update needed */ }
    }

    static async Task UpsertClusterRoleAsync(Kubernetes k8s, CancellationToken ct)
    {
        var cr = BuildClusterRole();
        try { await k8s.CreateClusterRoleAsync(cr, cancellationToken: ct); }
        catch (Exception ex) when (IsAlreadyExists(ex))
        {
            // Replace so permissions stay up-to-date on re-runs
            await k8s.ReplaceClusterRoleAsync(cr, ClusterRoleName, cancellationToken: ct);
        }
    }

    static async Task UpsertClusterRoleBindingAsync(
        Kubernetes k8s, string saName, string saNamespace, CancellationToken ct)
    {
        var crb = BuildClusterRoleBinding(saName, saNamespace);
        try { await k8s.CreateClusterRoleBindingAsync(crb, cancellationToken: ct); }
        catch (Exception ex) when (IsAlreadyExists(ex))
        {
            await k8s.ReplaceClusterRoleBindingAsync(crb, ClusterRoleBindingName, cancellationToken: ct);
        }
    }

    static async Task EnsureTokenSecretAsync(
        Kubernetes k8s, string secretName, string saName, string ns, CancellationToken ct)
    {
        var secret = new V1Secret
        {
            Metadata = new V1ObjectMeta
            {
                Name              = secretName,
                NamespaceProperty = ns,
                Labels            = ManagedByLabel(),
                Annotations       = new Dictionary<string, string>
                    { ["kubernetes.io/service-account.name"] = saName }
            },
            Type = "kubernetes.io/service-account-token"
        };
        try { await k8s.CreateNamespacedSecretAsync(secret, ns, cancellationToken: ct); }
        catch (Exception ex) when (IsAlreadyExists(ex)) { /* already exists — token is still valid */ }
    }

    /// <summary>
    /// Returns true if the k8s API responded with 409 Conflict / AlreadyExists.
    /// Works across all KubernetesClient versions (v10 dropped Microsoft.Rest).
    /// </summary>
    static bool IsAlreadyExists(Exception ex) =>
        ex.Message.Contains("AlreadyExists", StringComparison.OrdinalIgnoreCase) ||
        ex.Message.Contains("already exist",  StringComparison.OrdinalIgnoreCase) ||
        ex.Message.Contains("(Conflict)",     StringComparison.OrdinalIgnoreCase) ||
        ex.Message.Contains("409",            StringComparison.OrdinalIgnoreCase);

    static async Task<string> WaitForTokenAsync(
        Kubernetes k8s, string secretName, string ns, CancellationToken ct)
    {
        for (int i = 0; i < 30; i++)
        {
            var s = await k8s.ReadNamespacedSecretAsync(secretName, ns, cancellationToken: ct);
            if (s.Data != null && s.Data.TryGetValue("token", out var tokenBytes))
                return System.Text.Encoding.UTF8.GetString(tokenBytes);

            await Task.Delay(2_000, ct);
        }
        throw new TimeoutException(
            "Timed out waiting for the ServiceAccount token to be issued. " +
            "Check that the token controller is running in the target cluster.");
    }

    static string ExtractCaData(KubernetesClientConfiguration cfg)
    {
        if (cfg.SslCaCerts == null || cfg.SslCaCerts.Count == 0)
            return "";   // caller will set SkipTlsVerify on the remote client

        // Export the first CA cert as DER-encoded bytes and base64-encode them.
        // The MCP server's CreateRemoteClient() reverses this with Convert.FromBase64String.
        var derBytes = cfg.SslCaCerts[0].Export(X509ContentType.Cert);
        return Convert.ToBase64String(derBytes);
    }

    static Dictionary<string, string> ManagedByLabel() =>
        new() { ["app.kubernetes.io/managed-by"] = "kuberniq" };

    // ── RBAC manifests ─────────────────────────────────────────────────────────

    static V1ClusterRole BuildClusterRole() => new()
    {
        Metadata = new V1ObjectMeta { Name = ClusterRoleName, Labels = ManagedByLabel() },
        Rules =
        [
            new V1PolicyRule
            {
                ApiGroups = [""],
                Resources =
                [
                    "namespaces", "nodes", "pods", "pods/log", "services", "endpoints",
                    "events", "configmaps", "secrets", "serviceaccounts",
                    "persistentvolumes", "persistentvolumeclaims",
                    "resourcequotas", "limitranges"
                ],
                Verbs = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["apps"],
                Resources = ["deployments", "replicasets", "statefulsets", "daemonsets"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["batch"],
                Resources = ["jobs", "cronjobs"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["networking.k8s.io"],
                Resources = ["ingresses", "ingressclasses", "networkpolicies"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["rbac.authorization.k8s.io"],
                Resources = ["roles", "rolebindings", "clusterroles", "clusterrolebindings"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["autoscaling"],
                Resources = ["horizontalpodautoscalers"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["storage.k8s.io"],
                Resources = ["storageclasses", "volumeattachments"],
                Verbs     = ["get", "list", "watch"]
            },
            new V1PolicyRule
            {
                ApiGroups = ["policy"],
                Resources = ["poddisruptionbudgets"],
                Verbs     = ["get", "list", "watch"]
            },
        ]
    };

    static V1ClusterRoleBinding BuildClusterRoleBinding(string saName, string saNamespace) => new()
    {
        Metadata = new V1ObjectMeta { Name = ClusterRoleBindingName, Labels = ManagedByLabel() },
        RoleRef  = new V1RoleRef
        {
            ApiGroup = "rbac.authorization.k8s.io",
            Kind     = "ClusterRole",
            Name     = ClusterRoleName
        },
        Subjects =
        [
            new V1Subject
            {
                Kind             = "ServiceAccount",
                Name             = saName,
                NamespaceProperty = saNamespace
            }
        ]
    };

    /// <summary>List kubeconfig context names for the interactive selector.</summary>
    public static List<string> ListKubeconfigContexts()
    {
        try
        {
            var kubeconfigPath = Environment.GetEnvironmentVariable("KUBECONFIG")
                ?? Path.Combine(
                       Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                       ".kube", "config");

            var raw = KubernetesClientConfiguration.LoadKubeConfig(new FileInfo(kubeconfigPath));
            return raw.Contexts?.Select(c => c.Name).ToList() ?? [];
        }
        catch { return []; }
    }
}
