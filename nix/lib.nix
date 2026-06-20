
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
        REPORT="- [\`$LIB_FILE:$HASH_LINE\`]($LIB_URL): \`$OLD_HASH\` → \`$NEW_HASH\` — lockfile: [\`$LOCK_FILE\`]($LOCK_URL)"$'\\n'
      else
        REPORT="- \`$LIB_FILE:$HASH_LINE\`: \`$OLD_HASH\` → \`$NEW_HASH\`"$'\\n'
      fi

      if [ "$MODE" = "--apply" ]; then
        sed -i -E "s|npmDepsHash = \"sha256-[^\"]+\";|npmDepsHash = \"$NEW_HASH\";|" "$LIB_FILE"
        if ! nix build ".#${attr}.npmDeps" --no-link --print-build-logs 2>/dev/null; then
          # prefetch-npm-deps may disagree with fetchNpmDeps (it hashes
          # the lockfile contents, not the full source tree).  Extract the
          # correct hash from the nix build error and retry.
          RETRY_OUTPUT=$(nix build ".#${attr}.npmDeps" --no-link --print-build-logs 2>&1)
          CORRECT_HASH=$(echo "$RETRY_OUTPUT" | awk '/got:/ {print $2; exit}')
          if [ -n "$CORRECT_HASH" ]; then
            echo "prefetch-npm-deps gave $NEW_HASH but nix wants $CORRECT_HASH — retrying" >&2
            sed -i -E "s|npmDepsHash = \"sha256-[^\"]+\";|npmDepsHash = \"$CORRECT_HASH\";|" "$LIB_FILE"
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
