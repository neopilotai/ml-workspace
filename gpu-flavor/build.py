import argparse
import contextlib
import datetime
import subprocess

from ml_buildkit import build_utils
from ml_buildkit.helpers import build_docker

REMOTE_IMAGE_PREFIX = "khulnasoft/"
IMAGE_NAME = "ml-workspace"


def get_docker_image_name(flavor: str) -> str:
    """
    Constructs the Docker image name for a given flavor.
    
    Parameters:
        flavor (str): Flavor suffix to append to the base image name (e.g., "gpu").
    
    Returns:
        str: Image name formed by joining the base IMAGE_NAME, a hyphen, and the provided flavor.
    """
    return IMAGE_NAME + "-" + flavor


def get_base_image(version: str, release: bool) -> str:
    """
    Constructs the Docker base image reference for the given version.
    
    Parameters:
        release (bool): If True, prefix the image name with the remote repository prefix.
    
    Returns:
        str: Docker image reference, for example "ml-workspace:1.2.3" or "khulnasoft/ml-workspace:1.2.3".
    """
    base_image = f"{IMAGE_NAME}:{version}"
    if release:
        base_image = REMOTE_IMAGE_PREFIX + base_image
    return base_image


def get_build_args(flavor: str, version: str, vcs_ref: str, build_date: str) -> str:
    """
    Construct a Docker build-arg string for building the workspace image.
    
    Parameters:
        vcs_ref (str): VCS reference (e.g., short commit SHA) to embed as ARG_VCS_REF.
        build_date (str): Build timestamp (ISO 8601) to embed as ARG_BUILD_DATE.
    
    Returns:
        str: A concatenated string of `--build-arg` options setting ARG_WORKSPACE_BASE_IMAGE
             (base image for the given version, non-release), ARG_WORKSPACE_VERSION,
             ARG_WORKSPACE_FLAVOR, ARG_VCS_REF, and ARG_BUILD_DATE.
    """
    return (
        f" --build-arg ARG_WORKSPACE_BASE_IMAGE={get_base_image(version, False)}"
        f" --build-arg ARG_WORKSPACE_VERSION={version}"
        f" --build-arg ARG_WORKSPACE_FLAVOR={flavor}"
        f" --build-arg ARG_VCS_REF={vcs_ref}"
        f" --build-arg ARG_BUILD_DATE={build_date}"
    )


def main() -> None:
    """
    Parse build arguments and orchestrate building, testing, and releasing the Docker image for the selected flavor.
    
    Parses command-line arguments (including --flavor and common build flags), validates the flavor, determines VCS reference and build date, and then:
    - If the MAKE flag is set: builds the Docker image with appropriate build arguments.
    - If the TEST flag is set: runs a container from the built image, executes the test suite inside it, and removes the container; exits with code 1 on test failure.
    - If the RELEASE flag is set: publishes the Docker image using the configured image prefix.
    
    The function exits with code 1 on invalid flavor or when test failures occur.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--flavor",
        help="Flavor (gpu) used for docker container",
        default="gpu",
    )

    args = build_utils.parse_arguments(argument_parser=parser)

    version = str(args.get(build_utils.FLAG_VERSION))
    docker_image_prefix = args.get(build_docker.FLAG_DOCKER_IMAGE_PREFIX)

    if not docker_image_prefix:
        docker_image_prefix = REMOTE_IMAGE_PREFIX

    flavor = str(args.get("flavor")).lower().strip()

    if flavor not in ["gpu"]:
        build_utils.exit_process(1, "Unknown flavor")

    docker_image_name = get_docker_image_name(flavor)

    # Get the base image
    vcs_ref = "unknown"
    with contextlib.suppress(Exception):
        vcs_ref = (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"])
            .decode("ascii")
            .strip()
        )

    build_date = datetime.datetime.utcnow().isoformat("T") + "Z"
    with contextlib.suppress(Exception):
        build_date = (
            subprocess.check_output(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"])
            .decode("ascii")
            .strip()
        )

    if args.get(build_utils.FLAG_MAKE):
        build_args = get_build_args(flavor, version, vcs_ref, build_date)

        build_docker.build_docker_image(
            docker_image_name,
            version=version,
            build_args=build_args,
            exit_on_error=True,
        )

    if args.get(build_utils.FLAG_TEST):
        import docker

        workspace_name = f"workspace-test-{flavor}"
        workspace_port = "8080"
        client = docker.from_env(timeout=300)
        container = client.containers.run(
            f"{docker_image_name}:{version}",
            name=workspace_name,
            environment={
                "WORKSPACE_NAME": workspace_name,
                "WORKSPACE_ACCESS_PORT": workspace_port,
            },
            detach=True,
        )

        container.reload()
        container_ip = container.attrs["NetworkSettings"]["Networks"]["bridge"][
            "IPAddress"
        ]

        completed_process = build_utils.run(
            f"docker exec --env WORKSPACE_IP={container_ip} {workspace_name} "
            "pytest '/resources/tests'",
            exit_on_error=False,
        )

        container.remove(force=True)
        if completed_process.returncode > 0:
            build_utils.exit_process(1)

    if args.get(build_utils.FLAG_RELEASE):
        build_docker.release_docker_image(
            docker_image_name, version, docker_image_prefix, exit_on_error=True
        )


if __name__ == "__main__":
    main()