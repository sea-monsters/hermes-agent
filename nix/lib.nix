# nix/lib.nix — Shared helpers for nix stuff
#
# All npm packages in this repo are workspace members sharing a single
# root package-lock.json.  mkNpmPassthru provides the shared npmDeps,
# npmRoot, and npmConfigHook so individual .nix files don't duplicate them.
#
# Source filters (pythonSrc, per-package npm srcs) reduce rebuild scope so
# that e.g. a .tsx change doesn't trigger a Python venv rebuild, and a .py
# change doesn't trigger a TUI/Web/Desktop rebuild.  Each derivation gets a
# filtered src that only includes files it actually needs, while keeping
# the repo-root directory layout intact for buildNpmPackage /
# npmConfigHook workspace resolution.
#
# mkNpmPassthru returns packageJsonPath (e.g. "ui-tui/package.json")
# instead of a per-package devShellHook.  The root devshell hook
# (mkNpmDevShellHook) collects all package.json paths, stamps them,
# and if any changed, runs a single `npm i --package-lock-only` from
# root to update the lockfile, then `npm ci` if the lockfile changed.
{
  lib,
  pkgs,
  npm-lockfile-fix,
  nodejs,
}:
let
  repoRoot = ./..;

  # ── npm workspace discovery ────────────────────────────────────────
  # Single source of truth: the `workspaces` field of the root
  # package.json.  Everything below (workspace package.json discovery,
  # the Python source's JS-dir exclusions) is derived from this so the
  # topology is never duplicated.  Add a workspace to package.json and
  # the nix build picks it up automatically.
  rootPackageJson = builtins.fromJSON (builtins.readFile (repoRoot + "/package.json"));

  # Expand a workspace glob (e.g. "apps/*") into concrete member dirs
  # relative to the repo root.  Only trailing "*" globs are supported —
  # that's all npm uses here.  Literal patterns (e.g. "ui-tui") pass
  # through unchanged.
  expandWorkspace =
    pattern:
    let
      parts = lib.splitString "/" pattern;
    in
    if lib.last parts == "*" then
      let
        parent = lib.concatStringsSep "/" (lib.init parts);
        entries = builtins.readDir (repoRoot + "/${parent}");
        dirs = lib.filterAttrs (_: t: t == "directory") entries;
      in
      map (d: "${parent}/${d}") (builtins.attrNames dirs)
    else
      [ pattern ];

  # All workspace member directories (relative paths), filtered to those
  # that actually carry a package.json — a glob like apps/* may match a
  # dir that isn't really a package.
  workspaceMemberDirs = builtins.filter (d: builtins.pathExists (repoRoot + "/${d}/package.json")) (
    lib.concatMap expandWorkspace rootPackageJson.workspaces
  );

  # Top-level directory of each workspace member, deduplicated.  Used to
  # exclude JS/TS workspace trees from the Python source filter.  E.g.
  # apps/desktop + apps/shared + ui-tui + web → [ "apps" "ui-tui" "web" ].
  jsWorkspaceTopDirs = lib.unique (map (d: builtins.head (lib.splitString "/" d)) workspaceMemberDirs);

  # ── Source filters for reducing rebuild scope ──────────────────────
  # Changing a .tsx/.mjs file should NOT trigger a Python venv rebuild,
  # and changing a .py file should NOT trigger a TUI/Web/Desktop rebuild.

  # Python source: everything except JS/TS/docs/infra directories.
  pythonSrc = lib.cleanSourceWith {
    src = repoRoot;
    name = "hermes-python-source";
    filter =
      path: type:
      let
        relPath = lib.removePrefix (toString repoRoot + "/") (toString path);
        components = lib.splitString "/" relPath;
        topComponent = if components == [ ] then "" else builtins.head components;
        excludedDirs =
          # JS/TS workspace directories — derived from the npm workspaces
          # so a new workspace member is excluded from the Python source
          # without touching this list.
          jsWorkspaceTopDirs
          ++ [
            # Documentation
            "docs"
            "website"
            # CI/infra
            "docker"
            ".github"
            # Content/examples
            "infographic"
            "datagen-config-examples"
            # unused packaging infra
            "packaging"
            # Test infrastructure
            "tests"
            # Plan/temp files
            "plans"
            # Nix build definitions (Python build doesn't need these)
            "nix"
            # Skills are shipped via HERMES_BUNDLED_SKILLS /
            # HERMES_OPTIONAL_SKILLS (see hermes-agent.nix), not via the
            # wheel's data_files — setup.py's _data_file_tree returns []
            # for a missing dir, so the wheel builds fine without them.
            # This keeps SKILL.md edits from rebuilding the Python venv.
            # NOTE: optional-mcps must stay — pyproject.toml lists its
            # manifests as explicit data-files, which error when missing.
            "skills"
            "optional-skills"
          ];
        excludedFiles = [
          # JS root manifests
          "package.json"
          "package-lock.json"
          # Docker files
          "Dockerfile"
          "docker-compose.yml"
          "docker-compose.windows.yml"
          # Nix build definitions — editing the flake shouldn't rebuild
          # the venv.  (Input changes rebuild regardless, via the lock.)
          "flake.nix"
          "flake.lock"
          # Root docs the wheel doesn't consume.  README.md and LICENSE
          # must stay — pyproject.toml references them (readme /
          # license-files).
          "AGENTS.md"
          "CONTRIBUTING.md"
          "SECURITY.md"
          "README.zh-CN.md"
          ".gitignore"
          "setup-hermes.sh"
        ];
      in
      if relPath == "" then
        true
      else if builtins.elem relPath excludedFiles then
        false
      else if builtins.elem topComponent excludedDirs then
        false
      else
        true;
  };

  # Common npm workspace resolution files needed by all npm builds.
  # npm ci requires all workspace package.json files to resolve
  # workspace: protocol dependencies correctly.  Discovered from the
  # root package.json workspaces — root manifests + every member's
  # package.json.
  npmWorkspaceFiles = lib.fileset.unions (
    [
      (repoRoot + "/package.json")
      (repoRoot + "/package-lock.json")
    ]
    ++ map (d: repoRoot + "/${d}/package.json") workspaceMemberDirs
  );

  # npm deps source: just what importNpmLock needs (root manifests +
  # workspace member package.jsons).  Much smaller than the full repo,
  # so changing source files won't invalidate the npmDeps derivation.
  npmDepsSrc = lib.fileset.toSource {
    root = repoRoot;
    fileset = npmWorkspaceFiles;
  };

  # npm dependencies for the workspace, shared by all members. importNpmLock
  # resolves each package from the lockfile's own `integrity` hashes, so the
  # lockfile is the single source of truth — no separate dependency hash to
  # keep in sync with it.
  npmDeps = pkgs.importNpmLock.importNpmLock { npmRoot = npmDepsSrc; };

  # Build a per-package npm source: workspace resolution files + the
  # package's own directory tree(s).  Source ROOT is always the repo
  # root, preserving the workspace layout that buildNpmPackage and
  # npmConfigHook expect.  Callers pass the dirs they need (relative to
  # the repo root), so each package owns its own source scope.
  mkNpmSrc =
    dirs:
    lib.fileset.toSource {
      root = repoRoot;
      fileset = lib.fileset.union npmWorkspaceFiles (
        lib.fileset.unions (map (d: repoRoot + "/${d}") dirs)
      );
    };
in
{
  inherit pythonSrc;

  # Regenerate the shared root lockfile from scratch and verify all npm
  # packages still build.  Exposed as a runnable package — `nix run
  # .#update-npm-lockfile` — so it's actually usable, unlike a bin buried
  # in a build sandbox's PATH.  All workspace packages share one lockfile,
  # so there's a single script (not one per package).
  updateNpmLockfile = pkgs.writeShellScriptBin "update-npm-lockfile" ''
    set -euo pipefail
    # DEBUG=1 nix run .#update-npm-lockfile — trace every command
    [ -n "''${DEBUG:-}" ] && set -x

    REPO_ROOT=$(git rev-parse --show-toplevel)
    cd "$REPO_ROOT"

    rm -rf node_modules/
    ${pkgs.lib.getExe' nodejs "npm"} cache clean --force
    CI=true ${pkgs.lib.getExe' nodejs "npm"} install --workspaces
    ${pkgs.lib.getExe npm-lockfile-fix} ./package-lock.json

    # importNpmLock reads hashes from the lockfile itself — rebuild every
    # npm package to verify the new lockfile resolves offline.
    nix build .#tui .#web .#desktop
    echo "Lockfile updated and all npm packages built."
  '';

  # Returns a buildNpmPackage-compatible attrs set that provides:
  #   src, npmDeps, npmRoot      — filtered workspace source + importNpmLock dep set
  #   npmConfigHook              — importNpmLock's offline `npm install` hook
  #   passthru.packageJsonPath   — relative path to this workspace's package.json
  #   nodejs                     — fixed nodejs version for all packages we use in the repo
  #
  # `dirs` is the single source of truth for what the package contains:
  # its first entry is the package's own folder (→ packageJsonPath), and
  # all entries scope the filtered src.  Packages that import source from
  # another workspace member (file: deps) must list that member's dir too,
  # e.g. apps/desktop depends on apps/shared.
  #
  # Usage:
  #   npm = hermesNpmLib.mkNpmPassthru { dirs = [ "ui-tui" ]; };
  #   npm = hermesNpmLib.mkNpmPassthru { dirs = [ "apps/desktop" "apps/shared" ]; };
  #   pkgs.buildNpmPackage (npm // {
  #     pname = "hermes-tui";
  #     inherit version;
  #     buildPhase = '' ... '';
  #     installPhase = '' ... '';
  #   })
  mkNpmPassthru =
    { dirs }:
    let
      # The package's own folder is the first dir; it carries the
      # package.json that buildNpmPackage reads.
      folder = builtins.head dirs;
      # No sourceRoot — the workspace root (with the single package-lock.json)
      # is auto-detected as sourceRoot by nix.  npmRoot stays at "."
      # so npmConfigHook finds the lockfile there.
    in
    {
      inherit nodejs npmDeps;
      src = mkNpmSrc dirs;
      # importNpmLock's hook installs the rewritten lockfile (every `resolved`
      # rewritten to a /nix/store file: path) into the unpacked workspace and
      # runs `npm install` offline, so every workspace member's dependencies
      # resolve without network access.
      npmConfigHook = pkgs.importNpmLock.npmConfigHook;
      npmRoot = ".";

      ELECTRON_SKIP_BINARY_DOWNLOAD = 1;

      passthru = {
        packageJsonPath = "${folder}/package.json";
      };
    };

  # Single devshell hook for all npm workspace packages.
  #
  # Takes a list of package.json relative paths (from mkNpmPassthru .passthru.packageJsonPath),
  # stamps all of them, and if any changed:
  #   1. Runs `npm i --package-lock-only` from root to update the lockfile
  #   2. If the lockfile changed, runs `npm ci`
  mkNpmDevShellHook =
    packageJsonPaths:
    pkgs.writeShellScript "npm-dev-hook" ''
      REPO_ROOT=$(git rev-parse --show-toplevel)

      # Stamp all workspace package.jsons into one file.
      STAMP_DIR=".nix-stamps"
      STAMP="$STAMP_DIR/npm-package-jsons"
      STAMP_VALUE=$(
        ${pkgs.coreutils}/bin/sha256sum ${
          pkgs.lib.concatMapStringsSep " " (p: "\"$REPO_ROOT/${p}\"") packageJsonPaths
        } 2>/dev/null | ${pkgs.coreutils}/bin/sort | ${pkgs.coreutils}/bin/sha256sum | awk '{print $1}'
      )

      PKG_CHANGED=false
      if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$STAMP_VALUE" ]; then
        PKG_CHANGED=true
        echo "npm: package.json changed, updating lockfile..."
        ( cd "$REPO_ROOT" && ${pkgs.lib.getExe' nodejs "npm"} i --package-lock-only --silent --no-fund --no-audit 2>/dev/null )
        mkdir -p "$STAMP_DIR"
        echo "$STAMP_VALUE" > "$STAMP"
      fi

      # Check if lockfile changed (either from the npm i above or from an
      # external edit).  Runs npm ci if so.
      LOCK_STAMP="$STAMP_DIR/root-lockfile"
      LOCK_STAMP_VALUE=$(sha256sum "$REPO_ROOT/package-lock.json" 2>/dev/null | awk '{print $1}')
      if [ ! -f "$LOCK_STAMP" ] || [ "$(cat "$LOCK_STAMP")" != "$LOCK_STAMP_VALUE" ]; then
        echo "npm: package-lock.json changed, running npm ci..."
        ( cd "$REPO_ROOT" && CI=true ${pkgs.lib.getExe' nodejs "npm"} ci --silent --no-fund --no-audit 2>/dev/null )
        mkdir -p "$STAMP_DIR"
        echo "$LOCK_STAMP_VALUE" > "$STAMP"
      fi
    '';

  # Build `fix-lockfiles` bin that checks/updates the single npmDepsHash
  #   fix-lockfiles --check   # exit 1 if any hash is stale
  #   fix-lockfiles --apply   # rewrite stale hashes in place
  #   fix-lockfiles           # alias of --apply
  # Writes machine-readable fields (stale, changed, report) to $GITHUB_OUTPUT
  # when set, so CI workflows can post a sticky PR comment directly.
  mkFixLockfiles =
    {
      attr, # flake package attr for fallback verification build, e.g. "tui"
    }:
    pkgs.writeShellScriptBin "fix-lockfiles" ''
      set -uox pipefail
      MODE="''${1:---apply}"
      case "$MODE" in
        --check|--apply) ;;
        -h|--help)
          echo "usage: fix-lockfiles [--check|--apply]"
          exit 0 ;;
        *)
          echo "usage: fix-lockfiles [--check|--apply]" >&2
          exit 2 ;;
      esac

      REPO_ROOT="$(git rev-parse --show-toplevel)"
      cd "$REPO_ROOT"

      # When running in GH Actions, emit Markdown links in the report pointing
      # at the offending line of the nix file (and the lockfile) at the exact
      # commit that was checked. LINK_SHA should be set by the workflow to the
      # PR head SHA; falls back to GITHUB_SHA (which on pull_request is the
      # test-merge commit, still browseable).
      LINK_SERVER="''${GITHUB_SERVER_URL:-https://github.com}"
      LINK_REPO="''${GITHUB_REPOSITORY:-}"
      LINK_SHA="''${LINK_SHA:-''${GITHUB_SHA:-}}"

      STALE=0
      FIXED=0
      REPORT=""

      # All workspace packages share the root package-lock.json, so
      # we only need to check the hash once.
      LOCK_FILE="package-lock.json"
      LIB_FILE="nix/lib.nix"
      NEW_HASH=$(${pkgs.lib.getExe pkgs.prefetch-npm-deps} "$LOCK_FILE" 2>/dev/null)
      if [ -z "$NEW_HASH" ]; then
        echo "prefetch-npm-deps failed, falling back to nix build" >&2
        OUTPUT=$(nix build ".#${attr}.npmDeps" --no-link --print-build-logs 2>&1)
        STATUS=$?
        if [ "$STATUS" -eq 0 ]; then
          echo "ok (via nix build)"
          exit 0
        fi
        NEW_HASH=$(echo "$OUTPUT" | awk '/got:/ {print $2; exit}')
        if [ -z "$NEW_HASH" ]; then
          if echo "$OUTPUT" | grep -qE "throttled|HTTP error 418|substituter .* is disabled|some outputs of .* are not valid"; then
            echo "skipped (transient cache failure — see primary nix build for real status)" >&2
            echo "$OUTPUT" | tail -8 >&2
            exit 0
          fi
          echo "build failed with no hash mismatch:" >&2
          echo "$OUTPUT" | tail -40 >&2
          exit 1
        fi
      fi

      OLD_HASH=$(grep -oE 'npmDepsHash = "sha256-[^"]+"' "$LIB_FILE" | head -1 \
        | sed -E 's/npmDepsHash = "(.*)"/\1/')

      # prefetch-npm-deps says the hash already matches — but it only hashes the
      # lockfile *contents* and can disagree with fetchNpmDeps + npmConfigHook,
      # which validate the full source lockfile against the realized deps cache.
      # Trusting prefetch alone produced false "ok" results while the actual
      # build was broken (e.g. lockfile engines/os/cpu fields the pinned nixpkgs
      # strips from the deps cache, tripping npmConfigHook). So when prefetch
      # claims the hash is current, confirm with a real consumer build before
      # believing it.
      if [ "$NEW_HASH" = "$OLD_HASH" ]; then
        if VERIFY_OUT=$(nix build ".#${attr}" --no-link --print-build-logs 2>&1); then
          echo "ok"
          if [ -n "''${GITHUB_OUTPUT:-}" ]; then
            { echo "stale=false"; echo "changed=false"; } >> "$GITHUB_OUTPUT"
          fi
          exit 0
        fi
        # Build failed despite a matching hash. A fixed-output 'got:' means
        # prefetch genuinely disagreed with fetchNpmDeps — adopt the real hash
        # and fall through to the stale-handling path below.
        CORRECT_HASH=$(echo "$VERIFY_OUT" | awk '/got:/ {print $2; exit}')
        if [ -n "$CORRECT_HASH" ]; then
          echo "prefetch-npm-deps reported current ($OLD_HASH) but fetchNpmDeps wants $CORRECT_HASH" >&2
          NEW_HASH="$CORRECT_HASH"
        elif echo "$VERIFY_OUT" | grep -qE "throttled|HTTP error 418|substituter .* is disabled|some outputs of .* are not valid"; then
          echo "skipped (transient cache failure — see primary nix build for real status)" >&2
          echo "$VERIFY_OUT" | tail -8 >&2
          exit 0
        else
          # Not a stale-hash problem — surface it honestly instead of "ok".
          echo "::error::nix build .#${attr} failed and it is NOT a stale npmDepsHash (no 'got:' hash in output)." >&2
          echo "The committed lockfile may be incompatible with the pinned nixpkgs" >&2
          echo "(e.g. engines/os/cpu fields that prefetch-npm-deps strips from the" >&2
          echo "deps cache, tripping npmConfigHook). fix-lockfiles cannot repair this." >&2
          echo "$VERIFY_OUT" | tail -40 >&2
          if [ -n "''${GITHUB_OUTPUT:-}" ]; then
            { echo "stale=false"; echo "changed=false"; } >> "$GITHUB_OUTPUT"
          fi
          exit 1
        fi
      fi

      HASH_LINE=$(grep -n 'npmDepsHash = "sha256-' "$LIB_FILE" | head -1 | cut -d: -f1)
      echo "stale: $LIB_FILE:$HASH_LINE $OLD_HASH -> $NEW_HASH"
      STALE=1

      if [ -n "$LINK_REPO" ] && [ -n "$LINK_SHA" ]; then
        LIB_URL="$LINK_SERVER/$LINK_REPO/blob/$LINK_SHA/$LIB_FILE#L$HASH_LINE"
        LOCK_URL="$LINK_SERVER/$LINK_REPO/blob/$LINK_SHA/$LOCK_FILE"
        REPORT="- [\\`$LIB_FILE:$HASH_LINE\\`]($LIB_URL): \\`$OLD_HASH\\` → \\`$NEW_HASH\\` — lockfile: [\\`$LOCK_FILE\\`]($LOCK_URL)"$'\\\\n'
      else
        REPORT="- \\`$LIB_FILE:$HASH_LINE\\`: \\`$OLD_HASH\\` → \\`$NEW_HASH\\`"$'\\\\n'
      fi

      if [ "$MODE" = "--apply" ]; then
        sed -i -E "s|npmDepsHash = \\\"sha256-[^\\\"]+\\\";|npmDepsHash = \\\"$NEW_HASH\\\";|" "$LIB_FILE"
        if ! nix build ".#${attr}.npmDeps" --no-link --print-build-logs 2>/dev/null; then
          # prefetch-npm-deps may disagree with fetchNpmDeps (it hashes
          # the lockfile contents, not the full source tree).  Extract the
          # correct hash from the nix build error and retry.
          RETRY_OUTPUT=$(nix build ".#${attr}.npmDeps" --no-link --print-build-logs 2>&1)
          CORRECT_HASH=$(echo "$RETRY_OUTPUT" | awk '/got:/ {print $2; exit}')
          if [ -n "$CORRECT_HASH" ]; then
            echo "prefetch-npm-deps gave $NEW_HASH but nix wants $CORRECT_HASH — retrying" >&2
            sed -i -E "s|npmDepsHash = \\\"sha256-[^\\\"]+\\\";|npmDepsHash = \\\"$CORRECT_HASH\\\";|" "$LIB_FILE"
            if ! nix build ".#${attr}.npmDeps" --no-link --print-build-logs; then
              echo "verification build failed after hash retry" >&2
              exit 1
            fi
            NEW_HASH="$CORRECT_HASH"
          else
            echo "verification build failed after hash update" >&2
            exit 1
          fi
        fi
        FIXED=1
        echo "fixed"
      fi

      if [ -n "''${GITHUB_OUTPUT:-}" ]; then
        {
          [ "$STALE" -eq 1 ] && echo "stale=true" || echo "stale=false"
          [ "$FIXED" -eq 1 ] && echo "changed=true" || echo "changed=false"
          if [ -n "$REPORT" ]; then
            echo "report<<REPORT_EOF"
            printf "%s\n" "$REPORT"
            echo "REPORT_EOF"
          fi
        } >> "$GITHUB_OUTPUT"
      fi

      if [ "$STALE" -eq 1 ] && [ "$MODE" = "--check" ]; then
        echo
        echo "Stale lockfile hash detected. Run:"
        echo "  nix run .#fix-lockfiles"
        exit 1
      fi

      exit 0
    '';
}
