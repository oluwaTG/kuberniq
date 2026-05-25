using k8s;
using k8s.Models;

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
// Supported authorities (Authority field):
//   Entra ID / Azure AD : https://login.microsoftonline.com/{tenantId}/v2.0
//   AWS Cognito         : https://cognito-idp.{region}.amazonaws.com/{userPoolId}
//   Google              : https://accounts.google.com
//   Okta                : https://{domain}/oauth2/default
//   Any OIDC provider   : the issuer URL (must expose /.well-known/openid-configuration)
// ─────────────────────────────────────────────────────────────────────────────

public class OidcConfig
{
    const string SecretName = "kuberniq-oidc-config";

    /// <summary>Whether external OIDC authentication is enabled.</summary>
    public bool   Enabled         { get; private set; }

    /// <summary>OIDC issuer / authority URL (e.g. https://login.microsoftonline.com/{tenant}/v2.0)</summary>
    public string Authority       { get; private set; } = "";

    /// <summary>OAuth2 client ID registered with the provider.</summary>
    public string ClientId        { get; private set; } = "";

    /// <summary>OAuth2 client secret (used for Authorization Code flow callbacks).</summary>
    public string ClientSecret    { get; private set; } = "";

    /// <summary>
    /// JWT claim type that carries role/group information.
    /// Common values: "roles" (Entra ID), "cognito:groups" (Cognito), "groups" (Okta/generic).
    /// Defaults to "roles".
    /// </summary>
    public string RoleClaimType   { get; private set; } = "roles";

    /// <summary>
    /// Comma-separated claim values that should map to the 'admin' kuberniq role.
    /// e.g. "kuberniq-admins,GlobalAdmins"
    /// </summary>
    public string[] AdminValues    { get; private set; } = [];

    /// <summary>
    /// Comma-separated claim values that should map to the 'operator' kuberniq role.
    /// </summary>
    public string[] OperatorValues { get; private set; } = [];

    /// <summary>
    /// Role to assign when no matching group claim is found.
    /// Defaults to 'viewer'.
    /// </summary>
    public string DefaultRole     { get; private set; } = "viewer";

    // ── Loader ────────────────────────────────────────────────────────────────

    public static async Task<OidcConfig> LoadAsync(Kubernetes k8s, string ns, ILogger log)
    {
        var cfg = new OidcConfig();
        try
        {
            var secret = await k8s.ReadNamespacedSecretAsync(SecretName, ns);
            string Get(string key) =>
                secret.Data.TryGetValue(key, out var v) ? System.Text.Encoding.UTF8.GetString(v) : "";

            cfg.Enabled         = Get("enabled").Equals("true", StringComparison.OrdinalIgnoreCase);
            cfg.Authority       = Get("authority").TrimEnd('/');
            cfg.ClientId        = Get("clientId");
            cfg.ClientSecret    = Get("clientSecret");
            cfg.RoleClaimType   = string.IsNullOrWhiteSpace(Get("roleClaimType")) ? "roles" : Get("roleClaimType");
            cfg.DefaultRole     = string.IsNullOrWhiteSpace(Get("defaultRole"))   ? "viewer" : Get("defaultRole");
            cfg.AdminValues     = Get("adminValues").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
            cfg.OperatorValues  = Get("operatorValues").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

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

    /// <summary>
    /// Maps a set of provider group/role claim values to a kuberniq role.
    /// Returns null if no matching mapping found (caller should use DefaultRole).
    /// </summary>
    public string MapRole(IEnumerable<string> claimValues)
    {
        foreach (var v in claimValues)
        {
            if (AdminValues.Contains(v, StringComparer.OrdinalIgnoreCase))    return "admin";
            if (OperatorValues.Contains(v, StringComparer.OrdinalIgnoreCase)) return "operator";
        }
        return DefaultRole;
    }
}
