using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using k8s;
using k8s.Models;
using Microsoft.IdentityModel.Tokens;
using BC = BCrypt.Net.BCrypt;

namespace KuberniqServer;

// ── DTOs ─────────────────────────────────────────────────────────────────────
public record LoginRequest(string Username, string Password);
public record RefreshRequest(string RefreshToken);
public record CreateUserRequest(string Username, string Password, string? Role = "viewer");
public record ChangePasswordRequest(string CurrentPassword, string NewPassword);
public record TokenResponse(string AccessToken, string RefreshToken, int ExpiresIn);

// ── Auth service ──────────────────────────────────────────────────────────────
public class AuthService
{
    const string UserLabelType       = "kuberniq.io/type";
    const string UserLabelValue      = "user";
    const string RefreshLabelValue   = "refresh-token";
    const string SigningSecretName   = "kuberniq-jwt-signing-key";

    const int AccessTokenMinutes  = 60;        // 1 hour
    const int RefreshTokenDays    = 30;

    readonly Kubernetes  _k8s;
    readonly string      _ns;
    readonly ILogger     _log;
    string?              _signingKey;   // lazy-loaded

    public AuthService(Kubernetes k8s, string ns, ILogger log)
    {
        _k8s = k8s;
        _ns  = ns;
        _log = log;
    }

    // ── Signing key ───────────────────────────────────────────────────────────

    public async Task<string> GetOrCreateSigningKeyAsync()
    {
        if (_signingKey is not null) return _signingKey;

        try
        {
            var secret = await _k8s.ReadNamespacedSecretAsync(SigningSecretName, _ns);
            _signingKey = Encoding.UTF8.GetString(secret.Data["key"]);
            _log.LogInformation("[Auth] Loaded JWT signing key from secret.");
            return _signingKey;
        }
        catch (k8s.Autorest.HttpOperationException ex) when (ex.Response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            // First run — generate and persist a 512-bit key
            var key = Convert.ToBase64String(RandomNumberGenerator.GetBytes(64));
            var secret = new V1Secret
            {
                Metadata = new V1ObjectMeta
                {
                    Name              = SigningSecretName,
                    NamespaceProperty = _ns,
                    Labels            = new Dictionary<string, string>
                    {
                        ["kuberniq.io/type"] = "signing-key"
                    }
                },
                StringData = new Dictionary<string, string> { ["key"] = key }
            };
            await _k8s.CreateNamespacedSecretAsync(secret, _ns);
            _signingKey = key;
            _log.LogInformation("[Auth] Generated and stored new JWT signing key.");
            return _signingKey;
        }
    }

    // ── User management ───────────────────────────────────────────────────────

    /// <summary>Returns true if no users exist yet (first-run bootstrap).</summary>
    public async Task<bool> HasNoUsersAsync()
    {
        var list = await _k8s.ListNamespacedSecretAsync(
            _ns, labelSelector: $"{UserLabelType}={UserLabelValue}");
        return list.Items.Count == 0;
    }

    /// <summary>
    /// ArgoCD-style bootstrap: if no users exist, auto-creates an 'admin' user
    /// with a random password stored in the 'kuberniq-admin-initial-password' Secret.
    /// Retrieve it with:
    ///   kubectl get secret kuberniq-admin-initial-password -n &lt;ns&gt; -o jsonpath='{.data.password}' | base64 -d
    /// Returns the plaintext password if bootstrapped, null if admin already exists.
    /// </summary>
    public async Task<string?> BootstrapAdminAsync()
    {
        if (!await HasNoUsersAsync()) return null;

        // Generate a 24-char alphanumeric password (URL-safe, easily copy-pasteable)
        var raw      = RandomNumberGenerator.GetBytes(18);
        var password = Convert.ToBase64String(raw)
                              .Replace("+", "A").Replace("/", "B").Replace("=", "");

        // Persist the plaintext password as a recoverable Secret (like argocd-initial-admin-secret)
        const string initialSecretName = "kuberniq-admin-initial-password";
        try
        {
            var recoverSecret = new V1Secret
            {
                Metadata = new V1ObjectMeta
                {
                    Name              = initialSecretName,
                    NamespaceProperty = _ns,
                    Labels            = new Dictionary<string, string>
                    {
                        ["kuberniq.io/type"] = "initial-admin-password"
                    },
                    Annotations = new Dictionary<string, string>
                    {
                        ["kuberniq.io/note"] =
                            "Delete this secret after you have changed the admin password."
                    }
                },
                StringData = new Dictionary<string, string> { ["password"] = password }
            };
            await _k8s.CreateNamespacedSecretAsync(recoverSecret, _ns);
        }
        catch (k8s.Autorest.HttpOperationException ex)
            when (ex.Response.StatusCode == System.Net.HttpStatusCode.Conflict)
        {
            // Secret already exists from a previous partial bootstrap — just re-read it
            var existing = await _k8s.ReadNamespacedSecretAsync(initialSecretName, _ns);
            password = Encoding.UTF8.GetString(existing.Data["password"]);
        }

        var (ok, err) = await CreateUserAsync("admin", password, "admin");
        if (!ok)
            _log.LogWarning("[Auth] Bootstrap admin creation failed: {Error}", err);
        else
            _log.LogInformation("[Auth] Bootstrap admin user created.");

        return ok ? password : null;
    }

    public async Task<(bool ok, string error)> CreateUserAsync(
        string username, string password, string role = "viewer")
    {
        if (string.IsNullOrWhiteSpace(username) || username.Length < 3)
            return (false, "Username must be at least 3 characters.");
        if (string.IsNullOrWhiteSpace(password) || password.Length < 8)
            return (false, "Password must be at least 8 characters.");
        if (role is not ("admin" or "viewer"))
            return (false, "Role must be 'admin' or 'viewer'.");

        var secretName = UserSecretName(username);

        // Check for existing user
        try
        {
            await _k8s.ReadNamespacedSecretAsync(secretName, _ns);
            return (false, $"User '{username}' already exists.");
        }
        catch (k8s.Autorest.HttpOperationException ex) when (ex.Response.StatusCode == System.Net.HttpStatusCode.NotFound) { }

        var hash = BC.HashPassword(password, workFactor: 12);
        var secret = new V1Secret
        {
            Metadata = new V1ObjectMeta
            {
                Name              = secretName,
                NamespaceProperty = _ns,
                Labels            = new Dictionary<string, string>
                {
                    [UserLabelType]          = UserLabelValue,
                    ["kuberniq.io/username"] = username,
                    ["kuberniq.io/role"]     = role
                }
            },
            StringData = new Dictionary<string, string>
            {
                ["username"] = username,
                ["hash"]     = hash,
                ["role"]     = role
            }
        };
        await _k8s.CreateNamespacedSecretAsync(secret, _ns);
        _log.LogInformation("[Auth] Created user '{Username}' with role '{Role}'.", username, role);
        return (true, "");
    }

    public async Task<(bool ok, string error)> DeleteUserAsync(string username)
    {
        try
        {
            await _k8s.DeleteNamespacedSecretAsync(UserSecretName(username), _ns);
            // Also clean up any refresh tokens for this user
            var tokens = await _k8s.ListNamespacedSecretAsync(
                _ns,
                labelSelector: $"{UserLabelType}={RefreshLabelValue},kuberniq.io/username={username}");
            foreach (var t in tokens.Items)
                try { await _k8s.DeleteNamespacedSecretAsync(t.Metadata.Name, _ns); } catch { }

            _log.LogInformation("[Auth] Deleted user '{Username}'.", username);
            return (true, "");
        }
        catch (k8s.Autorest.HttpOperationException ex) when (ex.Response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return (false, $"User '{username}' not found.");
        }
    }

    public async Task<List<object>> ListUsersAsync()
    {
        var list = await _k8s.ListNamespacedSecretAsync(
            _ns, labelSelector: $"{UserLabelType}={UserLabelValue}");
        return list.Items.Select(s => (object)new
        {
            username = s.Metadata.Labels.TryGetValue("kuberniq.io/username", out var u) ? u : "?",
            role     = s.Metadata.Labels.TryGetValue("kuberniq.io/role",     out var r) ? r : "viewer"
        }).ToList();
    }

    public async Task<(bool ok, string error)> ChangePasswordAsync(
        string username, string currentPassword, string newPassword)
    {
        var (valid, _, _, _) = await ValidateCredentialsAsync(username, currentPassword);
        if (!valid) return (false, "Current password is incorrect.");
        if (newPassword.Length < 8) return (false, "New password must be at least 8 characters.");

        var secretName = UserSecretName(username);
        var secret     = await _k8s.ReadNamespacedSecretAsync(secretName, _ns);
        var newHash    = BC.HashPassword(newPassword, workFactor: 12);

        secret.StringData = new Dictionary<string, string>
        {
            ["username"] = username,
            ["hash"]     = newHash,
            ["role"]     = Encoding.UTF8.GetString(secret.Data["role"])
        };
        secret.Data = null;   // clear raw data so StringData wins
        await _k8s.ReplaceNamespacedSecretAsync(secret, secretName, _ns);
        return (true, "");
    }

    // ── Login / token issuance ────────────────────────────────────────────────

    public async Task<(bool valid, string username, string role, string error)>
        ValidateCredentialsAsync(string username, string password)
    {
        try
        {
            var secret = await _k8s.ReadNamespacedSecretAsync(UserSecretName(username), _ns);
            var hash   = Encoding.UTF8.GetString(secret.Data["hash"]);
            var role   = Encoding.UTF8.GetString(secret.Data["role"]);

            if (!BC.Verify(password, hash))
                return (false, "", "", "Invalid username or password.");

            return (true, username, role, "");
        }
        catch (k8s.Autorest.HttpOperationException ex) when (ex.Response.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            return (false, "", "", "Invalid username or password.");
        }
    }

    public async Task<TokenResponse> IssueTokensAsync(string username, string role)
    {
        var key       = await GetOrCreateSigningKeyAsync();
        var accessJwt = CreateAccessToken(username, role, key);
        var refresh   = await CreateRefreshTokenAsync(username);
        return new TokenResponse(accessJwt, refresh, AccessTokenMinutes * 60);
    }

    public async Task<(bool ok, TokenResponse? tokens, string error)>
        RefreshAsync(string refreshToken)
    {
        // Find the refresh token secret by value hash
        var tokenHash  = HashRefreshToken(refreshToken);
        var list       = await _k8s.ListNamespacedSecretAsync(
            _ns, labelSelector: $"{UserLabelType}={RefreshLabelValue}");

        V1Secret? found = null;
        foreach (var s in list.Items)
        {
            if (!s.Data.ContainsKey("tokenHash")) continue;
            var stored = Encoding.UTF8.GetString(s.Data["tokenHash"]);
            if (stored == tokenHash) { found = s; break; }
        }

        if (found is null) return (false, null, "Invalid or expired refresh token.");

        // Check expiry
        var expiresAt = DateTimeOffset.Parse(
            Encoding.UTF8.GetString(found.Data["expiresAt"]));
        if (DateTimeOffset.UtcNow > expiresAt)
        {
            await _k8s.DeleteNamespacedSecretAsync(found.Metadata.Name, _ns);
            return (false, null, "Refresh token has expired. Please log in again.");
        }

        var username = Encoding.UTF8.GetString(found.Data["username"]);
        var role     = Encoding.UTF8.GetString(found.Data["role"]);

        // Rotate: delete old, issue new
        await _k8s.DeleteNamespacedSecretAsync(found.Metadata.Name, _ns);
        var tokens = await IssueTokensAsync(username, role);
        return (true, tokens, "");
    }

    public async Task<bool> RevokeRefreshTokenAsync(string refreshToken)
    {
        var tokenHash = HashRefreshToken(refreshToken);
        var list      = await _k8s.ListNamespacedSecretAsync(
            _ns, labelSelector: $"{UserLabelType}={RefreshLabelValue}");

        foreach (var s in list.Items)
        {
            if (!s.Data.ContainsKey("tokenHash")) continue;
            if (Encoding.UTF8.GetString(s.Data["tokenHash"]) == tokenHash)
            {
                await _k8s.DeleteNamespacedSecretAsync(s.Metadata.Name, _ns);
                return true;
            }
        }
        return false;
    }

    // ── JWT validation (used by middleware) ───────────────────────────────────

    public async Task<ClaimsPrincipal?> ValidateAccessTokenAsync(string token)
    {
        var key = await GetOrCreateSigningKeyAsync();
        var tvp = new TokenValidationParameters
        {
            ValidateIssuer           = true,
            ValidIssuer              = "kuberniq-server",
            ValidateAudience         = true,
            ValidAudience            = "kuberniq-cli",
            ValidateLifetime         = true,
            IssuerSigningKey         = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(key)),
            ClockSkew                = TimeSpan.FromSeconds(30)
        };
        try
        {
            var handler   = new JwtSecurityTokenHandler();
            var principal = handler.ValidateToken(token, tvp, out _);
            return principal;
        }
        catch { return null; }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    string CreateAccessToken(string username, string role, string signingKey)
    {
        var key     = new SymmetricSecurityKey(Encoding.UTF8.GetBytes(signingKey));
        var creds   = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
        var expires = DateTime.UtcNow.AddMinutes(AccessTokenMinutes);

        var token = new JwtSecurityToken(
            issuer:             "kuberniq-server",
            audience:           "kuberniq-cli",
            claims:
            [
                new Claim(ClaimTypes.Name,  username),
                new Claim(ClaimTypes.Role,  role),
                new Claim("sub",            username)
            ],
            expires:            expires,
            signingCredentials: creds);

        return new JwtSecurityTokenHandler().WriteToken(token);
    }

    async Task<string> CreateRefreshTokenAsync(string username)
    {
        // Look up the role from the user secret
        string role = "viewer";
        try
        {
            var userSecret = await _k8s.ReadNamespacedSecretAsync(UserSecretName(username), _ns);
            role = Encoding.UTF8.GetString(userSecret.Data["role"]);
        }
        catch { }

        var rawToken  = Convert.ToBase64String(RandomNumberGenerator.GetBytes(48));
        var tokenHash = HashRefreshToken(rawToken);
        var expiresAt = DateTimeOffset.UtcNow.AddDays(RefreshTokenDays);

        var secretName = $"kuberniq-rt-{Guid.NewGuid():N}";
        var secret = new V1Secret
        {
            Metadata = new V1ObjectMeta
            {
                Name              = secretName,
                NamespaceProperty = _ns,
                Labels            = new Dictionary<string, string>
                {
                    [UserLabelType]          = RefreshLabelValue,
                    ["kuberniq.io/username"] = username
                }
            },
            StringData = new Dictionary<string, string>
            {
                ["tokenHash"] = tokenHash,
                ["username"]  = username,
                ["role"]      = role,
                ["expiresAt"] = expiresAt.ToString("O")
            }
        };
        await _k8s.CreateNamespacedSecretAsync(secret, _ns);
        return rawToken;
    }

    static string HashRefreshToken(string raw)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(raw));
        return Convert.ToBase64String(bytes);
    }

    static string UserSecretName(string username) =>
        $"kuberniq-user-{username.ToLowerInvariant().Replace(" ", "-")}";
}
