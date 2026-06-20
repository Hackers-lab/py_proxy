# Releasing Net Split-Tunneler

How to cut a new release. Follow this **every time** you publish a version.
The app self-updates from GitHub Releases, so getting these steps right matters —
a mistake here can put every user into an update loop.

---

## TL;DR checklist

1. [ ] Bump the version in **both** places (see below). They must match.
2. [ ] Add a new section to [CHANGELOG.md](CHANGELOG.md) describing *what changed*.
3. [ ] Commit everything to `main`.
4. [ ] Tag the commit `vX.Y.Z` (same number as the version) and push the tag.
5. [ ] Watch the Action go green: https://github.com/Hackers-lab/py_proxy/actions
6. [ ] Confirm the release has **exactly one** `NetSplitTunnel_Setup_vX.Y.Z.exe` asset.

```bash
# after editing the two version files + CHANGELOG.md:
git add -A
git commit -m "vX.Y.Z: <one-line summary>"
git push origin main
git tag vX.Y.Z
git push origin vX.Y.Z
```

---

## The version lives in TWO places — keep them identical

| File | What | Authority |
|------|------|-----------|
| `nst/__init__.py` → `__version__ = "X.Y.Z"` | The app's real version. CI reads this to name the build and the installer, and the running app compares it against the latest release. | **Primary — this is the source of truth.** |
| `installer.iss` → `#define AppVersion "X.Y.Z"` | Fallback used only when ISCC is run by hand. CI overrides it with `/DAppVersion=<read from __init__.py>`. | Secondary — keep in sync to avoid confusing local builds. |

> The git **tag** (`vX.Y.Z`) should also equal this number. The tag names the
> GitHub release; the installer filename comes from `__version__`. If the tag is
> higher than `__version__`, older code paths can think they're perpetually
> behind. Always: `__version__` == `installer.iss` AppVersion == tag (minus the `v`).

**The version must strictly increase every release.** The updater installs an
update only when the latest release's installer has a *higher* version than the
running app. Re-using or lowering a number breaks updates.

---

## Every release needs its own changelog entry

Add a new section to the top of [CHANGELOG.md](CHANGELOG.md):

```markdown
## vX.Y.Z — YYYY-MM-DD
- What changed, in plain language (one bullet per user-visible change).
- Mention fixes, new settings, behaviour changes.
```

This is the human-readable "what's in this update". Write it as you make the
change, not after — it's easy to forget.

> Note: the release **notes shown on GitHub** currently come from `README.md`
> (see `body_path:` in `.github/workflows/release.yml`). If you want each
> release to show its own changelog instead, point `body_path` at a per-version
> notes file. For now, CHANGELOG.md is the canonical record.

---

## What the CI does (so you don't have to)

On `git push` of a `v*` tag, `.github/workflows/release.yml`:
1. Reads `__version__` from `nst/__init__.py`.
2. Builds the **one-folder** app: `pyinstaller NetSplitTunnel.spec --noconfirm --clean`.
3. Compiles the per-user installer: `ISCC /DAppVersion=<version> installer.iss`
   → `Output/NetSplitTunnel_Setup_vX.Y.Z.exe` (Output/ is wiped first).
4. Publishes a GitHub Release with **only** that one installer attached.

You should never run any of this by hand for a release — just push the tag.

---

## Hard rules (these have bitten us before)

- **Never commit build artifacts.** `build/`, `dist/`, `Output/`, `build.log`,
  and `version_info.txt` are git-ignored. A committed `Output/...Setup.exe`
  caused the release to ship two installers and put users in an **endless update
  loop** (the updater grabbed the stale one). Keep them ignored.
- **One installer asset per release.** A release must contain exactly one
  `NetSplitTunnel_Setup_v*.exe`. If you ever see two, delete the stale one in the
  GitHub Releases UI immediately — clients may be looping.
- **Don't change the installer `AppId` GUID** in `installer.iss`. It's what lets
  a new version upgrade the old one in place instead of installing side-by-side.
- **Test the update path with a *higher* version.** To verify auto-update, the
  installed app must be a *lower* version than the published release.

---

## Local build / test (optional, no push)

```powershell
pyinstaller NetSplitTunnel.spec --noconfirm --clean
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=X.Y.Z installer.iss
# Installs to %LOCALAPPDATA%\Programs\NetSplitTunnel (no admin):
Output\NetSplitTunnel_Setup_vX.Y.Z.exe
```

See also: the project memory note "Distribution & update" for the architecture
(no-admin app, per-user installer, silent self-update, never commit build/dist).
