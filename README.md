# IOBEWI Rust development environment

Disposable Docker-based embedded Rust workspace for VS Code Dev Containers or CLI development. IDLab is maintained separately at https://github.com/iobewi/idlab.

## Usage

Clone this repository into WSL2, open it in VS Code and run **Dev Containers: Reopen in Container**. Clone application repositories into the mounted workspace; their Git history is independent. The workspace is persisted on the host. Rebuilding the container does not remove it. The Claude configuration volume is intentionally retained; do not store project secrets in the image.

For CLI use, build with `docker build -f docker/Dockerfile -t rust-dev .` and run `docker run --rm -it --mount type=bind,source="$PWD",target=/workspace rust-dev`. Run `rust-dev-smoke` to verify toolchains. No USB access or privileged container is required. Install the separate IDLab client in projects that need hardware qualification.

The CI builds and starts the devcontainer and checks installed tool versions. Application compilation and tests belong to application repositories. No hardware service is started.
