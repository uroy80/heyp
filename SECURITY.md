# Security policy

heyp processes people's photos, so security reports are very welcome.

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability**
(<https://github.com/uroy80/heyp/security/advisories/new>). Do not open a public issue, and do not attach
anyone's real photos — a minimal synthetic file is enough.

Include what you can: affected version or commit (`/api/health` shows the version; the footer of `/privacy` links
the commit), the tool and request, what an attacker gains, and a proof of concept. You will get an
acknowledgement within 7 days and a fix or a plan within 30 days for confirmed issues. Credit in the advisory is
yours if you want it. We won't take legal action over good-faith research that follows this policy.

## Supported versions

Only the latest release and the current `main` branch get security fixes. The public instance (heyp.edgr.in) runs
`main`.

## Scope

In scope: the web app and API, the container images on `ghcr.io/uroy80/` (`heyp`, `heyp-renderer`, `heyp-egress`),
the deployment files in `deploy/` and the GitHub Actions workflows. Especially interesting:

- reading another visitor's uploads or results, or files outliving their expiry (idle and hard TTL)
- server-side request forgery through the HTML → image tool, or escaping the renderer's isolated network and the
  egress proxy's allow-list (private, link-local and the server's own addresses must stay unreachable)
- code execution or resource exhaustion through crafted images and animations (frame bombs, decompression bombs,
  malicious SVG, PSD or camera RAW files)
- getting file contents, file names or client IPs into logs beyond what `/privacy` says
- bypassing the upload, pixel, job or capture limits, or spoofing the client IP they're keyed on
- anything that lets a pull request, fork or third-party action reach secrets or the production server

Out of scope: volumetric denial of service, missing headers without a concrete impact, and findings in
third-party services. Please test against your own instance (`docker compose up`), not the public site. heypdf is a
separate project with its own policy (github.com/uroy80/heypdf).

## Where the boundaries are

Untrusted camera RAW and SVG files are decoded by native engines (LibRaw, resvg) in short-lived child processes
(`app/core/sandbox.py`, `app/core/confine.py`). What a code-execution bug in one of them can reach:

- **Resources:** each child has memory, CPU-time and file-size limits and a wall-clock timeout, and dumps no core.
- **Secrets:** secret-looking variables (`*_TOKEN`, `*_API_KEY`, …) are removed from the app's environment before
  any child starts, and again inside the child; the app process is not dumpable, so a child (same uid) can't read
  its `/proc/<pid>/environ` or memory.
- **Files (Landlock, Linux ≥ 5.13):** a child sees nothing of the file store (`HEYP_TMP_DIR`) except its own inputs
  (read) and its job's working directory (read/write); the rest of the filesystem keeps the container's access.
  Where the kernel or filesystem can't apply Landlock (checked once at run time), this layer is skipped and logged.
- **Network:** a seccomp filter makes `socket()` fail in children (also `ptrace`, `process_vm_*`, `pidfd_getfd` and
  io_uring); Landlock ABI ≥ 4/6 also denies TCP and scopes signals and abstract Unix sockets.

Every other format is decoded in-process by Pillow, pillow-heif and psd-tools with the pixel, frame and size limits
checked before decoding. The container is the outer boundary: non-root uid, read-only root filesystem, no
capabilities, `no-new-privileges`, the default seccomp profile, and the renderer on an internal network whose only
way out is the egress proxy. `HEYP_SANDBOX_CONFINE=0` turns the Landlock/seccomp layer off for debugging; the
resource limits and the secret scrub always apply.

## How releases are protected

Every image is built by GitHub Actions on GitHub-hosted runners from a commit on `main`, and carries a signed
SLSA provenance and an SBOM attestation; the deploy job verifies both before the server pulls anything. Verify an
image before you run it:

```bash
gh attestation verify oci://ghcr.io/uroy80/heyp:<tag> -R uroy80/heyp
```
