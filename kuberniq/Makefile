# =============================================================================
# kuberniq — build / install targets
# =============================================================================
# Usage:
#   make                  # build for the current machine (auto-detects OS + arch)
#   make install          # build + copy to /usr/local/bin  (macOS / Linux)
#   make windows          # cross-compile a Windows .exe
#   make all-platforms    # build for macOS arm64, macOS x64, Linux x64, Windows x64
#   make clean            # remove dist/
# =============================================================================

BINARY   := kuberniq
DIST     := dist
CONFIG   := Release

# ── Detect current platform ──────────────────────────────────────────────────
UNAME_S := $(shell uname -s 2>/dev/null || echo Windows)
UNAME_M := $(shell uname -m 2>/dev/null || echo x86_64)

ifeq ($(UNAME_S),Darwin)
  ifeq ($(UNAME_M),arm64)
    CURRENT_RID := osx-arm64
  else
    CURRENT_RID := osx-x64
  endif
else ifeq ($(UNAME_S),Linux)
  ifeq ($(UNAME_M),aarch64)
    CURRENT_RID := linux-arm64
  else
    CURRENT_RID := linux-x64
  endif
else
  CURRENT_RID := win-x64
endif

# ── Default: build for current platform ──────────────────────────────────────
.PHONY: build
build:
	dotnet publish -r $(CURRENT_RID) -c $(CONFIG) -o $(DIST)/$(CURRENT_RID)
	@echo ""
	@echo "✅  Binary: $(DIST)/$(CURRENT_RID)/$(BINARY)"

# ── install: build + copy to /usr/local/bin (macOS / Linux) ──────────────────
.PHONY: install
install: build
	cp $(DIST)/$(CURRENT_RID)/$(BINARY) /usr/local/bin/$(BINARY)
	chmod +x /usr/local/bin/$(BINARY)
	@echo "✅  Installed to /usr/local/bin/$(BINARY)"
	@echo "    Run: kuberniq --help"

# ── Platform-specific targets ─────────────────────────────────────────────────
.PHONY: macos-arm64
macos-arm64:
	dotnet publish -r osx-arm64   -c $(CONFIG) -o $(DIST)/osx-arm64

.PHONY: macos-x64
macos-x64:
	dotnet publish -r osx-x64     -c $(CONFIG) -o $(DIST)/osx-x64

.PHONY: linux-x64
linux-x64:
	dotnet publish -r linux-x64   -c $(CONFIG) -o $(DIST)/linux-x64

.PHONY: linux-arm64
linux-arm64:
	dotnet publish -r linux-arm64 -c $(CONFIG) -o $(DIST)/linux-arm64

.PHONY: windows
windows:
	dotnet publish -r win-x64     -c $(CONFIG) -o $(DIST)/win-x64
	@echo "✅  Windows binary: $(DIST)/win-x64/$(BINARY).exe"

.PHONY: all-platforms
all-platforms: macos-arm64 macos-x64 linux-x64 windows
	@echo ""
	@echo "✅  All platform binaries written to $(DIST)/"
	@ls -lh $(DIST)/*/$(BINARY)* 2>/dev/null || true

# ── Clean ─────────────────────────────────────────────────────────────────────
.PHONY: clean
clean:
	rm -rf $(DIST) bin obj
