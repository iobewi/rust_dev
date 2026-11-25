# IOBEWI Rust development environment

Disposable Docker-based embedded Rust workspace for VS Code Dev Containers or CLI development. IDLab is maintained separately at https://github.com/iobewi/idlab.

## Usage

Clone this repository into WSL2, open it in VS Code and run **Dev Containers: Reopen in Container**. VS Code uses the published image from GitHub Container Registry (GHCR). Clone application repositories into the mounted workspace; their Git history is independent. The workspace is persisted on the host. Recreating the container does not remove it. The Claude configuration volume is intentionally retained; do not store project secrets in the image.

The first GHCR publication must succeed before using the default devcontainer configuration. If the package is private, authenticate with GHCR using an authorized token with read:packages access.

For CLI use:

```sh
docker pull ghcr.io/iobewi/rust-dev:latest
docker run --rm -it --mount type=bind,source="$PWD",target=/workspace ghcr.io/iobewi/rust-dev:latest
```

Run `rust-dev-smoke` to verify installed tools. No USB access or privileged container is required. Install the separate IDLab client in projects that need hardware qualification.

## Build and distribution

To build locally, run `docker build -f docker/Dockerfile -t rust-dev .`. To use that local image in VS Code, set the devcontainer image to `rust-dev` instead of the GHCR reference.

CI builds the Docker image and checks installed tool versions. Application compilation and tests belong to application repositories. No hardware service is started. Pull requests never publish images. Successful builds on main publish both `latest` and `sha-<full commit SHA>`. Use the SHA tag or the image digest to select a specific build; use a digest for immutable reproducibility. The initial distribution targets linux/amd64.

To refresh an existing VS Code environment, pull the new image and use **Dev Containers: Rebuild Container**.

## Line endings

The image defaults to `core.autocrlf=input` and `core.eol=lf` in Git's system configuration. VS Code uses LF for new files. Project `.gitattributes` and explicit Git user or repository settings may override these defaults. Existing checkouts are not rewritten automatically.
