using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text.Json;
using Microsoft.IdentityModel.Tokens;

namespace KuberniqServer;

// ── OIDC token validator ──────────────────────────────────────────────────────
// Validates JWTs issued by an external OIDC provider by:
//   1. Fetching the provider's JWKS URI from /.well-known/openid-configuration
//   2. Caching the signing keys (refreshed every 24h or on key-not-found)
//   3. Validating the token signature, issuer, audience, and expiry
//   4. Mapping group/role claims to a kuberniq role via OidcConfig.MapRole()
//
// Usage in middleware:
//   var result = await _oidcValidator.ValidateAsync(rawToken);
//   if (result is not null) { /* use result.Username and result.Role */ }
// ─────────────────────────────────────────────────────────────────────────────

public record OidcValidationResult(string Username, string Role);

public class OidcValidator
{
    readonly OidcConfig           _cfg;
    readonly ILogger              _log;
    readonly HttpClient           _http;
    readonly JwtSecurityTokenHandler _handler = new();

    JsonWebKeySet? _jwks;
    DateTime       _jwksExpiry = DateTime.MinValue;
    string?        _jwksUri;
    string?        _resolvedIssuer;   // actual issuer from discovery (may differ from Authority)

    static readonly TimeSpan JwksCacheDuration = TimeSpan.FromHours(24);

    public OidcValidator(OidcConfig cfg, ILogger log)
    {
        _cfg  = cfg;
        _log  = log;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
    }

    // ── Public API ────────────────────────────────────────────────────────────

    /// <summary>
    /// Validates an external OIDC JWT. Returns null if validation fails or OIDC is disabled.
    /// </summary>
    public async Task<OidcValidationResult?> ValidateAsync(string rawToken)
    {
        if (!_cfg.Enabled || string.IsNullOrWhiteSpace(_cfg.Authority))
            return null;

        try
        {
            var keys = await GetSigningKeysAsync();
            if (keys is null) return null;

            var parameters = new TokenValidationParameters
            {
                ValidateIssuer           = true,
                ValidIssuers             = GetValidIssuers(),
                ValidateAudience         = !string.IsNullOrWhiteSpace(_cfg.ClientId),
                ValidAudiences           = string.IsNullOrWhiteSpace(_cfg.ClientId)
                                             ? null
                                             : [_cfg.ClientId, $"api://{_cfg.ClientId}"],
                ValidateLifetime         = true,
                IssuerSigningKeys        = keys,
                ValidateIssuerSigningKey = true,
                ClockSkew                = TimeSpan.FromSeconds(30),
            };

            ClaimsPrincipal principal;
            try
            {
                principal = _handler.ValidateToken(rawToken, parameters, out _);
            }
            catch (SecurityTokenSignatureKeyNotFoundException)
            {
                // Signing keys may have rotated — force refresh and retry once
                _log.LogInformation("[OIDC] Signing key not found — refreshing JWKS and retrying.");
                _jwksExpiry = DateTime.MinValue;
                keys = await GetSigningKeysAsync();
                if (keys is null) return null;
                parameters.IssuerSigningKeys = keys;
                principal = _handler.ValidateToken(rawToken, parameters, out _);
            }

            var username = ExtractUsername(principal);
            var role     = ExtractRole(principal);

            _log.LogInformation("[OIDC] Validated external token for '{Username}' → role '{Role}'", username, role);
            return new OidcValidationResult(username, role);
        }
        catch (SecurityTokenExpiredException)
        {
            _log.LogDebug("[OIDC] Token expired.");
            return null;
        }
        catch (Exception ex)
        {
            _log.LogDebug(ex, "[OIDC] Token validation failed.");
            return null;
        }
    }

    // ── Private helpers ───────────────────────────────────────────────────────

    string[] GetValidIssuers()
    {
        // Include both the raw Authority and the discovered issuer (they can differ slightly)
        var list = new List<string> { _cfg.Authority };
        if (_resolvedIssuer is not null && !list.Contains(_resolvedIssuer))
            list.Add(_resolvedIssuer);
        return [.. list];
    }

    string ExtractUsername(ClaimsPrincipal principal)
    {
        // Prefer email, then upn, then preferred_username, then sub
        return principal.FindFirst("email")?.Value
            ?? principal.FindFirst("upn")?.Value
            ?? principal.FindFirst("preferred_username")?.Value
            ?? principal.FindFirst(ClaimTypes.Email)?.Value
            ?? principal.FindFirst(ClaimTypes.Upn)?.Value
            ?? principal.FindFirst(ClaimTypes.NameIdentifier)?.Value
            ?? principal.FindFirst("sub")?.Value
            ?? "unknown";
    }

    string ExtractRole(ClaimsPrincipal principal)
    {
        // Collect all values for the configured role claim type
        var claimValues = principal.FindAll(_cfg.RoleClaimType)
            .Select(c => c.Value)
            .ToList();

        // Also check standard ClaimTypes.Role in case the provider maps it
        claimValues.AddRange(principal.FindAll(ClaimTypes.Role).Select(c => c.Value));

        return _cfg.MapRole(claimValues);
    }

    async Task<IEnumerable<SecurityKey>?> GetSigningKeysAsync()
    {
        if (_jwks is not null && DateTime.UtcNow < _jwksExpiry)
            return _jwks.GetSigningKeys();

        try
        {
            // Step 1: discover the JWKS URI from the provider's well-known endpoint
            if (_jwksUri is null)
            {
                var discoveryUrl = $"{_cfg.Authority}/.well-known/openid-configuration";
                _log.LogInformation("[OIDC] Fetching discovery document from {Url}", discoveryUrl);

                var discoveryJson = await _http.GetStringAsync(discoveryUrl);
                using var doc = JsonDocument.Parse(discoveryJson);

                _jwksUri = doc.RootElement.GetProperty("jwks_uri").GetString()
                    ?? throw new InvalidOperationException("JWKS URI not found in discovery document.");

                if (doc.RootElement.TryGetProperty("issuer", out var issuerEl))
                    _resolvedIssuer = issuerEl.GetString();

                _log.LogInformation("[OIDC] Discovered JWKS URI: {Uri}", _jwksUri);
            }

            // Step 2: fetch and cache the JWKS
            var jwksJson = await _http.GetStringAsync(_jwksUri);
            _jwks        = new JsonWebKeySet(jwksJson);
            _jwksExpiry  = DateTime.UtcNow + JwksCacheDuration;

            _log.LogInformation("[OIDC] JWKS refreshed ({Count} keys cached).", _jwks.Keys.Count);
            return _jwks.GetSigningKeys();
        }
        catch (Exception ex)
        {
            _log.LogWarning(ex, "[OIDC] Failed to fetch signing keys from provider.");
            return null;
        }
    }
}
