using System.Diagnostics;
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
        var kubeconfigPath = Environment.GetEnvironmentVariable("KUBECONFIG")
            ?? Path.Combine(
                   Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                   ".kube", "config");

        // Cloud clusters (AKS, GKE, EKS) use exec-based credentials (kubelogin, gke-gcloud-auth-plugin, etc.)
        // The .NET SDK does not execute these plugins — delegate entirely to kubectl in that case.
        if (HasExecCredentials(context, kubeconfigPath))
        {
            progress.Report("Exec credentials detected — using kubectl for authentication");
            return await SetupWithKubectlAsync(context, saName, saNamespace, skipRbac, progress, ct);
        }

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
            progress.Report("Applying ClusterRole 'kuberniq-mcp-reader'");
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
            new Rbacv1Subject
            {
                Kind             = "ServiceAccount",
                Name             = saName,
                NamespaceProperty = saNamespace
            }
        ]
    };

    /// <summary>Returns the current-context name from kubeconfig (for display in cluster list).</summary>
    public static string? GetCurrentContext()
    {
        try
        {
            var kubeconfigPath = Environment.GetEnvironmentVariable("KUBECONFIG")
                ?? Path.Combine(
                       Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                       ".kube", "config");
            var raw = KubernetesClientConfiguration.LoadKubeConfig(new FileInfo(kubeconfigPath));
            return raw.CurrentContext;
        }
        catch { return null; }
    }

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

    // ── Exec-credential (cloud cluster) support ───────────────────────────────

    /// <summary>
    /// Returns true when the kubeconfig context uses an exec plugin (kubelogin, gke-gcloud-auth-plugin, etc.)
    /// The .NET SDK does not execute these plugins, so kubectl subprocess is used instead.
    /// Detection is done by scanning the raw YAML for an "exec:" key under the matching user.
    /// </summary>
    static bool HasExecCredentials(string context, string kubeconfigPath)
    {
        try
        {
            // Read the raw kubeconfig and look for "exec:" under the user entry for this context.
            // This avoids relying on SDK model property names which differ across versions.
            var yaml = File.ReadAllText(kubeconfigPath);

            // Quick pre-check — if "exec:" doesn't appear anywhere, no exec credentials at all
            if (!yaml.Contains("exec:", StringComparison.OrdinalIgnoreCase)) return false;

            var raw = KubernetesClientConfiguration.LoadKubeConfig(new FileInfo(kubeconfigPath));
            var ctx = raw.Contexts?.FirstOrDefault(c => c.Name == context);
            if (ctx is null) return false;

            var userName = ctx.ContextDetails.User;

            // Find the user's block in the YAML and check if it contains "exec:"
            // This is a pragmatic scan — good enough for all real-world kubeconfigs
            var lines   = yaml.Split('\n');
            bool inUser = false;
            int  indent = 0;

            for (int i = 0; i < lines.Length; i++)
            {
                var line = lines[i];

                if (!inUser)
                {
                    // Match "  - name: <userName>" inside the users: block
                    if (line.TrimStart().StartsWith("- name:") &&
                        line.Contains(userName))
                    {
                        inUser = true;
                        indent = line.Length - line.TrimStart().Length;
                    }
                }
                else
                {
                    // Stop if we've left this user's block (new list item or back to root)
                    var trimmed = line.TrimStart();
                    if (trimmed.Length > 0 && trimmed.StartsWith("- ") &&
                        (line.Length - trimmed.Length) <= indent)
                        break;

                    if (trimmed.StartsWith("exec:"))
                        return true;
                }
            }
            return false;
        }
        catch { return false; }
    }

    /// <summary>
    /// Full cluster setup using kubectl subprocesses — used for AKS, GKE, EKS and any
    /// other cluster whose kubeconfig context uses exec-based credential plugins.
    /// </summary>
    static async Task<(string server, string caData, string token)> SetupWithKubectlAsync(
        string context, string saName, string saNamespace, bool skipRbac,
        IProgress<string> progress, CancellationToken ct)
    {
        if (!skipRbac)
        {
            progress.Report($"Ensuring namespace '{saNamespace}' exists");
            await KubectlApplyAsync($"""
                apiVersion: v1
                kind: Namespace
                metadata:
                  name: {saNamespace}
                """, context, ct);

            progress.Report($"Creating ServiceAccount '{saName}'");
            await KubectlApplyAsync($"""
                apiVersion: v1
                kind: ServiceAccount
                metadata:
                  name: {saName}
                  namespace: {saNamespace}
                  labels:
                    app.kubernetes.io/managed-by: kuberniq
                """, context, ct);

            progress.Report("Applying ClusterRole 'kuberniq-mcp-reader'");
            await KubectlApplyAsync("""
                apiVersion: rbac.authorization.k8s.io/v1
                kind: ClusterRole
                metadata:
                  name: kuberniq-mcp-reader
                  labels:
                    app.kubernetes.io/managed-by: kuberniq
                rules:
                - apiGroups: [""]
                  resources: ["namespaces","pods","services","endpoints","events","configmaps",
                               "persistentvolumes","persistentvolumeclaims","nodes",
                               "resourcequotas","serviceaccounts","replicationcontrollers"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["apps"]
                  resources: ["deployments","replicasets","statefulsets","daemonsets"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["batch"]
                  resources: ["jobs","cronjobs"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["networking.k8s.io"]
                  resources: ["ingresses","networkpolicies"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["autoscaling"]
                  resources: ["horizontalpodautoscalers"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["storage.k8s.io"]
                  resources: ["storageclasses","volumeattachments"]
                  verbs: ["get","list","watch"]
                - apiGroups: ["policy"]
                  resources: ["poddisruptionbudgets"]
                  verbs: ["get","list","watch"]
                """, context, ct);

            progress.Report("Applying ClusterRoleBinding");
            await KubectlApplyAsync($"""
                apiVersion: rbac.authorization.k8s.io/v1
                kind: ClusterRoleBinding
                metadata:
                  name: kuberniq-mcp-reader
                  labels:
                    app.kubernetes.io/managed-by: kuberniq
                roleRef:
                  apiGroup: rbac.authorization.k8s.io
                  kind: ClusterRole
                  name: kuberniq-mcp-reader
                subjects:
                - kind: ServiceAccount
                  name: {saName}
                  namespace: {saNamespace}
                """, context, ct);
        }

        var tokenSecretName = $"{saName}-kuberniq-token";
        progress.Report($"Ensuring token Secret '{tokenSecretName}'");

        // service-account-token Secrets are owned by the token controller — applying over an
        // existing one can produce a conflict. Delete first so re-registration always works cleanly.
        try { await KubectlAsync(context, ct, "delete", "secret", tokenSecretName, "-n", saNamespace, "--ignore-not-found"); }
        catch { /* best-effort */ }

        await KubectlApplyAsync($"""
            apiVersion: v1
            kind: Secret
            metadata:
              name: {tokenSecretName}
              namespace: {saNamespace}
              annotations:
                kubernetes.io/service-account.name: {saName}
            type: kubernetes.io/service-account-token
            """, context, ct);

        // Wait for the token controller to populate the token
        progress.Report("Waiting for token to be issued by the token controller");
        string token = "";
        for (int i = 0; i < 30; i++)
        {
            try
            {
                var b64 = await KubectlAsync(context, ct,
                    "get", "secret", tokenSecretName,
                    "-n", saNamespace,
                    "-o", "jsonpath={.data.token}");
                if (!string.IsNullOrWhiteSpace(b64))
                {
                    token = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(b64));
                    break;
                }
            }
            catch { }
            await Task.Delay(2000, ct);
        }
        if (string.IsNullOrWhiteSpace(token))
            throw new Exception("Timed out waiting for ServiceAccount token.");

        var server = await KubectlAsync(context, ct,
            "config", "view", "--context", context, "--minify",
            "-o", "jsonpath={.clusters[0].cluster.server}");

        string caData = "";
        try
        {
            caData = await KubectlAsync(context, ct,
                "config", "view", "--context", context, "--minify", "--raw",
                "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}");
        }
        catch { /* CA data is optional */ }

        return (server, caData, token);
    }

    /// <summary>Run kubectl with the given context and args; returns stdout.</summary>
    static async Task<string> KubectlAsync(string context, CancellationToken ct, params string[] args)
    {
        var psi = new ProcessStartInfo("kubectl")
        {
            RedirectStandardOutput = true,
            RedirectStandardError  = true,
            UseShellExecute        = false
        };
        psi.ArgumentList.Add("--context");
        psi.ArgumentList.Add(context);
        foreach (var a in args) psi.ArgumentList.Add(a);

        using var proc = Process.Start(psi)
            ?? throw new Exception("Failed to start kubectl. Ensure kubectl is installed and on your PATH.");

        var stdout = await proc.StandardOutput.ReadToEndAsync(ct);
        var stderr = await proc.StandardError.ReadToEndAsync(ct);
        await proc.WaitForExitAsync(ct);

        if (proc.ExitCode != 0)
        {
            var msg = stderr.Trim().Length > 0 ? stderr.Trim() : $"kubectl exited with code {proc.ExitCode}";

            // Provide actionable hints for well-known auth failures
            if (msg.Contains("AADSTS70043") || msg.Contains("AADSTS70008") || msg.Contains("AADSTS700082"))
                throw new Exception(
                    $"Azure token has expired (AADSTS). Run [cyan]az login[/] to refresh your session, then retry.\n\nDetail: {msg}");

            if (msg.Contains("AADSTS") )
                throw new Exception(
                    $"Azure authentication error. Run [cyan]az login[/] and ensure you have access to the cluster.\n\nDetail: {msg}");

            if (msg.Contains("kubelogin") && msg.Contains("exit"))
                throw new Exception(
                    $"kubelogin failed. Ensure kubelogin is installed ([cyan]brew install Azure/kubelogin/kubelogin[/]) and run [cyan]az login[/].\n\nDetail: {msg}");

            throw new Exception(msg);
        }

        return stdout.Trim();
    }

    /// <summary>Write YAML to a temp file and run kubectl apply -f, then clean up.</summary>
    static async Task KubectlApplyAsync(string yaml, string context, CancellationToken ct)
    {
        var tmp = Path.Combine(Path.GetTempPath(), $"kuberniq-{Guid.NewGuid():N}.yaml");
        try
        {
            await File.WriteAllTextAsync(tmp, yaml, ct);
            await KubectlAsync(context, ct, "apply", "-f", tmp);
        }
        finally
        {
            try { File.Delete(tmp); } catch { }
        }
    }

    /// <summary>
    /// Delete all Kubernetes resources that were created by <see cref="SetupAsync"/>.
    /// Silently skips resources that are already gone (404).
    /// </summary>
    public static async Task TeardownAsync(
        string context,
        string saName,
        string saNamespace,
        IProgress<string> progress,
        CancellationToken ct = default)
    {
        // Always use kubectl — exec-credential clusters (AKS/GKE/EKS) can't use the SDK directly.
        async Task Delete(params string[] args)
        {
            try   { await KubectlAsync(context, ct, args); }
            catch (Exception ex) when (ex.Message.Contains("NotFound") || ex.Message.Contains("not found")) { }
        }

        var tokenSecretName = $"{saName}-kuberniq-token";

        progress.Report($"Deleting Secret '{tokenSecretName}'");
        await Delete("delete", "secret", tokenSecretName, "-n", saNamespace, "--ignore-not-found");

        progress.Report($"Deleting ServiceAccount '{saName}'");
        await Delete("delete", "serviceaccount", saName, "-n", saNamespace, "--ignore-not-found");

        progress.Report("Deleting ClusterRoleBinding 'kuberniq-mcp-reader'");
        await Delete("delete", "clusterrolebinding", "kuberniq-mcp-reader", "--ignore-not-found");

        progress.Report("Deleting ClusterRole 'kuberniq-mcp-reader'");
        await Delete("delete", "clusterrole", "kuberniq-mcp-reader", "--ignore-not-found");

        // Delete the namespace only if it was the default kuberniq-managed one
        if (saNamespace == "kuberniq-server")
        {
            progress.Report($"Deleting namespace '{saNamespace}'");
            await Delete("delete", "namespace", saNamespace, "--ignore-not-found");
        }
    }
}
