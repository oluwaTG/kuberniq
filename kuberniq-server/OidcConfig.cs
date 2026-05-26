using k8s;
using k8s.Models;
using System.Security.Cryptography;
using System.Text;

namespace KuberniqServer;

// ── OIDC provider configuration ───────────────────────────────────────────────
// Loaded from the K8s Secret 'kuberniq-oidc-config' in the MCP namespace.
// The secret is optional — if absent, external OIDC login is simply disabled.
//
// Example secret:
//   kubectl create secret generic kuberniq-oidc-config -n <ns> \
//     --from-literal=enabled=true \
//     --from-literal=authority=https://login.microsoftonline.com/<tenantId>/v2.0 \
//     --from-literal=clientId=<appId> \
//     --from-literal=clientSecret=<secret> \
//     --from-literal=defaultRole=viewer \
//     --from-literal=roleClaimType=roles \
//     --from-literal=adminValues=kuberniq-admins \
//     --from-literal=operatorValues=kuberniq-operators
//
// Redirect URI to register in Azure App Registration (Phase 2):
//   https://<your-host>/auth/oidc/callback
// ─────────────────────────────────────────────────────────────────────────────

public class OidcConfig
{
    const string SecretName = "kuberniq-oidc-config";

    public bool   Enabled         { get; private set; }
    public string Authority       { get; private set; } = "";
    public string ClientId        { get; private set; } = "";
    public string ClientSecret    { get; private set; } = "";
    public string RoleClaimType   { get; private set; } = "roles";
    public string[] AdminValues    { get; private set; } = [];
    public string[] OperatorValues { get; private set; } = [];
    public string DefaultRole     { get; private set; } = "viewer";

    /// <summary>
    /// Optional explicit redirect URI to use for the OIDC callback.
    /// Set this in the secret as 'redirectUri' to pin it to a known value —
    /// e.g. https://mcp-server.local/auth/oidc/callback
    /// When absent, the URI is derived dynamically from the incoming request.
    /// </summary>
    public string? RedirectUri { get; private set; }

    // Resolved from OIDC discovery document
    public string? AuthorizationEndpoint { get; set; }
    public string? TokenEndpoint         { get; set; }

    // ── Loader ────────────────────────────────────────────────────────────────

    public static async Task<OidcConfig> LoadAsync(Kubernetes k8s, string ns, ILogger log)
    {
        var cfg = new OidcConfig();
        try
        {
            var secret = await k8s.ReadNamespacedSecretAsync(SecretName, ns);
            string Get(string key) =>
                secret.Data.TryGetValue(key, out var v) ? Encoding.UTF8.GetString(v) : "";

            cfg.Enabled         = Get("enabled").Equals("true", StringComparison.OrdinalIgnoreCase);
            cfg.Authority       = Get("authority").TrimEnd('/');
            cfg.ClientId        = Get("clientId");
            cfg.ClientSecret    = Get("clientSecret");
            cfg.RoleClaimType   = string.IsNullOrWhiteSpace(Get("roleClaimType")) ? "roles" : Get("roleClaimType");
            cfg.DefaultRole     = string.IsNullOrWhiteSpace(Get("defaultRole"))   ? "viewer" : Get("defaultRole");
            cfg.AdminValues     = Get("adminValues").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            cfg.OperatorValues  = Get("operatorValues").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            cfg.RedirectUri     = string.IsNullOrWhiteSpace(Get("redirectUri")) ? null : Get("redirectUri");

            if (cfg.Enabled)
                log.LogInformation("[OIDC] External auth enabled. Authority: {Authority}", cfg.Authority);
            else
                log.LogInformation("[OIDC] Secret found but enabled=false. External auth disabled.");
        }
        catch (k8s.Autorest.HttpOperationException ex) when (ex.Response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            log.LogInformation("[OIDC] No '{SecretName}' secret found — external auth disabled.", SecretName);
        }
        catch (Exception ex)
        {
            log.LogWarning(ex, "[OIDC] Failed to load OIDC config — external auth disabled.");
        }
        return cfg;
    }

    public string MapRole(IEnumerable<string> claimValues)
    {
        foreach (var v in claimValues)
        {
            if (AdminValues.Contains(v, StringComparer.OrdinalIgnoreCase))    return "admin";
            if (OperatorValues.Contains(v, StringComparer.OrdinalIgnoreCase)) return "operator";
        }
        return DefaultRole;
    }

    // ── PKCE helpers ──────────────────────────────────────────────────────────

    public static string GenerateCodeVerifier()
    {
        var bytes = RandomNumberGenerator.GetBytes(64);
        return Base64UrlEncode(bytes);
    }

    public static string GenerateCodeChallenge(string verifier)
    {
        var hash = SHA256.HashData(Encoding.ASCII.GetBytes(verifier));
        return Base64UrlEncode(hash);
    }

    public static string GenerateState() =>
        Base64UrlEncode(RandomNumberGenerator.GetBytes(32));

    static string Base64UrlEncode(byte[] bytes) =>
        Convert.ToBase64String(bytes)
            .TrimEnd('=')
            .Replace('+', '-')
            .Replace('/', '_');
}
