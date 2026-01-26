import contextlib
import glob
import json
import os
import subprocess
import threading
import time
import warnings
from datetime import datetime
from subprocess import call

import git
import tornado
from notebook.base.handlers import IPythonHandler
from notebook.utils import url_path_join
from tornado import web

try:
    from urllib.parse import unquote
except ImportError:
    from urllib import unquote


SHARED_SSH_SETUP_PATH = "/shared/ssh/setup"
HOME = os.getenv("HOME", "/root")
RESOURCES_PATH = os.getenv("RESOURCES_PATH", "/resources")
WORKSPACE_HOME = os.getenv("WORKSPACE_HOME", "/workspace")
WORKSPACE_CONFIG_FOLDER = os.path.join(HOME, ".workspace")

MAX_WORKSPACE_FOLDER_SIZE = os.getenv("MAX_WORKSPACE_FOLDER_SIZE", None)
if MAX_WORKSPACE_FOLDER_SIZE and MAX_WORKSPACE_FOLDER_SIZE.isnumeric():
    MAX_WORKSPACE_FOLDER_SIZE = int(MAX_WORKSPACE_FOLDER_SIZE)
else:
    MAX_WORKSPACE_FOLDER_SIZE = None

MAX_CONTAINER_SIZE = os.getenv("MAX_CONTAINER_SIZE", None)
if MAX_CONTAINER_SIZE and MAX_CONTAINER_SIZE.isnumeric():
    MAX_CONTAINER_SIZE = int(MAX_CONTAINER_SIZE)
else:
    MAX_CONTAINER_SIZE = None


# -------------- HANDLER -------------------------


class HelloWorldHandler(IPythonHandler):
    def data_received(self, chunk):
        pass

    def get(self):
        result = self.request.protocol + "://" + self.request.host
        if "base_url" in self.application.settings:
            result = result + "   " + self.application.settings["base_url"]
        self.finish(result)


def handle_error(handler, status_code: int, error_msg: str = None, exception=None):
    handler.set_status(status_code)

    if not error_msg:
        error_msg = ""

    if exception:
        if error_msg:
            error_msg += "\nException: "

        error_msg += str(exception)

    error = {"error": error_msg}
    handler.finish(json.dumps(error))

    log.info("An error occurred (" + str(status_code) + "): " + error_msg)


def send_data(handler, data):
    handler.finish(json.dumps(data, sort_keys=True, indent=4))


class PingHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        # Used by Jupyterhub to test if user cookies are valid
        self.finish("Successful")


class InstallToolHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            workspace_installer_folder = RESOURCES_PATH + "/tools/"
            workspace_tool_installers = []

            # sort entries by name
            for f in sorted(
                glob.glob(os.path.join(workspace_installer_folder, "*.sh"))
            ):
                tool_name = os.path.splitext(os.path.basename(f))[0].strip()
                workspace_tool_installers.append(
                    {"name": tool_name, "command": "/bin/bash " + f}
                )

            if not workspace_tool_installers:
                log.warn(
                    "No workspace tool installers found at path: "
                    + workspace_installer_folder
                )
                # Backup if file does not exist
                workspace_tool_installers.append(
                    {
                        "name": "none",
                        "command": "No workspace tool installers found at path: "
                        + workspace_installer_folder,
                    }
                )
            self.finish(json.dumps(workspace_tool_installers))
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class ToolingHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        """
        Respond with the list of workspace tooling definitions discovered in HOME/.workspace/tools/ as a JSON array.
        
        Scans all `*.json` files in the workspace tooling folder, loads each file as either a single tool (dict) or a list of tools, and deduplicates entries by the `id` field. If no tools are found, returns a default VNC tool entry. Failures to load individual files are logged; unexpected errors are reported as an HTTP 500 response.
        """
        try:
            workspace_tooling_folder = HOME + "/.workspace/tools/"
            workspace_tools = []

            def tool_is_duplicated(tool_array, tool):
                """Tools with same ID should only be added once to the list"""
                for t in tool_array:
                    if "id" in t and "id" in tool and tool["id"] == t["id"]:
                        return True
                return False

            # sort entries by name
            for f in sorted(
                glob.glob(os.path.join(workspace_tooling_folder, "*.json"))
            ):
                try:
                    with open(f, "rb") as tool_file:
                        tool_data = json.load(tool_file)
                        if not tool_data:
                            continue
                        if isinstance(tool_data, dict):
                            if not tool_is_duplicated(workspace_tools, tool_data):
                                workspace_tools.append(tool_data)
                        else:
                            # tool data is probably an array
                            for tool in tool_data:
                                if not tool_is_duplicated(workspace_tools, tool):
                                    workspace_tools.append(tool)
                except Exception:
                    log.warn("Failed to load tools file: " + f.name)
                    continue

            if not workspace_tools:
                log.warn(
                    "No workspace tools found at path: " + workspace_tooling_folder
                )
                # Backup if file does not exist
                workspace_tools.append(
                    {
                        "id": "vnc-link",
                        "name": "VNC",
                        "url_path": "/tools/vnc/?password=vncpassword",
                        "description": "Desktop GUI for the workspace",
                    }
                )
            self.finish(json.dumps(workspace_tools))
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class GitCommitHandler(IPythonHandler):
    @web.authenticated
    def post(self):
        data = self.get_json_body()

        if data is None:
            handle_error(
                self, 400, "Please provide a valid file path and commit msg in body."
            )
            return

        if "filePath" not in data or not data["filePath"]:
            handle_error(self, 400, "Please provide a valid filePath in body.")
            return

        file_path = _resolve_path(unquote(data["filePath"]))

        commit_msg = None
        if "commitMsg" in data:
            commit_msg = unquote(data["commitMsg"])

        try:
            commit_file(file_path, commit_msg)
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class GitInfoHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            path = _resolve_path(self.get_argument("path", None))
            send_data(self, get_git_info(path))
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return

    @web.authenticated
    def post(self):
        """
        Set the Git user name and email for the repository located at the resolved request path.
        
        Expects a query parameter "path" (resolved against the server root) and a JSON body containing "name" and "email". Validates both fields and sets the repository's local git config values user.name and user.email. Responds with HTTP 400 if the body is missing or either field is invalid, and with HTTP 500 if an internal error occurs while locating the repository or applying the configuration.
        """
        path = _resolve_path(self.get_argument("path", None))
        data = self.get_json_body()

        if data is None:
            handle_error(self, 400, "Please provide a valid name and email in body.")
            return

        if "email" not in data or not data["email"]:
            handle_error(self, 400, "Please provide a valid email.")
            return

        email = data["email"]

        if "name" not in data or not data["name"]:
            handle_error(self, 400, "Please provide a valid name.")
            return

        name = data["name"]

        try:
            repo = get_repo(path)
            set_user_email(email, repo)
            set_user_name(name, repo)
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class SSHScriptHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            handle_ssh_script_request(self)
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class SharedSSHHandler(IPythonHandler):
    def get(self):
        # authentication only via token
        try:
            sharing_enabled = os.environ.get("SHARED_LINKS_ENABLED", "false")
            if sharing_enabled.lower() != "true":
                handle_error(
                    self,
                    401,
                    error_msg="Shared links are disabled. Please download and execute the SSH script manually.",
                )
                return

            token = self.get_argument("token", None)
            valid_token = generate_token(self.request.path)
            if not token:
                self.set_status(401)
                self.finish('echo "Please provide a token via get parameter."')
                return
            if token.lower().strip() != valid_token:
                self.set_status(401)
                self.finish('echo "The provided token is not valid."')
                return

            handle_ssh_script_request(self)
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class SSHCommandHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            sharing_enabled = os.environ.get("SHARED_LINKS_ENABLED", "false")
            if sharing_enabled.lower() != "true":
                self.finish(
                    "Shared links are disabled. Please download and executen the SSH script manually."
                )
                return

            # schema + host + port
            origin = self.get_argument("origin", None)
            if not origin:
                handle_error(
                    self,
                    400,
                    "Please provide a valid origin (endpoint url) via get parameter.",
                )
                return

            host, port = parse_endpoint_origin(origin)
            base_url = web_app.settings["base_url"].rstrip("/") + SHARED_SSH_SETUP_PATH
            setup_command = (
                '/bin/bash <(curl -s --insecure "'
                + origin
                + base_url
                + "?token="
                + generate_token(base_url)
                + "&host="
                + host
                + "&port="
                + port
                + '")'
            )

            self.finish(setup_command)
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class SharedTokenHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            sharing_enabled = os.environ.get("SHARED_LINKS_ENABLED", "false")
            if sharing_enabled.lower() != "true":
                handle_error(self, 400, error_msg="Shared links are disabled.")
                return

            path = self.get_argument("path", None)
            if path is None:
                handle_error(
                    self, 400, "Please provide a valid path via get parameter."
                )
                return

            self.finish(generate_token(path))
        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class SharedFilesHandler(IPythonHandler):
    @web.authenticated
    def get(self):
        try:
            sharing_enabled = os.environ.get("SHARED_LINKS_ENABLED", "false")
            if sharing_enabled.lower() != "true":
                self.finish(
                    "Shared links are disabled. Please download and share the data manually."
                )
                return

            path = _resolve_path(self.get_argument("path", None))
            if not path:
                handle_error(
                    self, 400, "Please provide a valid path via get parameter."
                )
                return

            if not os.path.exists(path):
                handle_error(
                    self, 400, "The selected file or folder does not exist: " + path
                )
                return

            # schema + host + port
            origin = self.get_argument("origin", None)
            if not origin:
                handle_error(
                    self,
                    400,
                    "Please provide a valid origin (endpoint url) via get parameter.",
                )
                return

            token = generate_token(path)

            try:
                # filebrowser needs to be stopped so that a user can be added
                call("supervisorctl stop filebrowser", shell=True)

                # Add new user with the given permissions and scope
                add_user_command = (
                    "filebrowser users add "
                    + token
                    + " "
                    + token
                    + " --perm.admin=false --perm.create=false --perm.delete=false"
                    + " --perm.download=true --perm.execute=false --perm.modify=false"
                    + " --perm.rename=false --perm.share=false --lockPassword=true"
                    + " --database="
                    + HOME
                    + '/filebrowser.db --scope="'
                    + path
                    + '"'
                )

                call(add_user_command, shell=True)
            except Exception:
                pass

            call("supervisorctl start filebrowser", shell=True)

            base_url = web_app.settings["base_url"].rstrip("/") + "/shared/filebrowser/"
            setup_command = origin + base_url + "?token=" + token
            self.finish(setup_command)

        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


class StorageCheckHandler(IPythonHandler):
    @web.authenticated
    def get(self) -> None:
        """
        Responds with current storage usage, configured limits, and warning flags for the container and workspace folder.
        
        Throttles costly metadata refreshes (checks are skipped if a recent update exists) and triggers a background metadata update when needed. The HTTP response is finished with a JSON object describing sizes (in GB), configured limits, and whether each limit is exceeded.
        
        Returns:
            A JSON object with the following keys:
            - `containerSize` (number): Current container size in gigabytes (rounded to 1 decimal) when a container limit is configured.
            - `containerSizeLimit` (number): Configured container size limit in gigabytes (rounded).
            - `containerSizeWarning` (boolean): `true` if the container size exceeds the configured limit, `false` otherwise.
            - `workspaceFolderSize` (number): Current workspace folder size in gigabytes (rounded to 1 decimal) when a workspace limit is configured.
            - `workspaceFolderSizeLimit` (number): Configured workspace folder size limit in gigabytes (rounded).
            - `workspaceFolderSizeWarning` (boolean): `true` if the workspace folder size exceeds the configured limit, `false` otherwise.
        
        When neither limit is configured, returns an object with both warning flags set to `false`.
        """
        try:
            CHECK_INTERVAL_MINUTES = 5

            result = {
                "workspaceFolderSizeWarning": False,
                "containerSizeWarning": False,
            }

            if not MAX_WORKSPACE_FOLDER_SIZE and not MAX_CONTAINER_SIZE:
                self.finish(json.dumps(result))
                return

            minutes_since_update = get_minutes_since_size_update()
            if (
                minutes_since_update is not None
                and minutes_since_update < CHECK_INTERVAL_MINUTES
            ):
                # only run check every 5 minutes
                self.finish(json.dumps(result))
                return

            # Only run update every two minutes
            # run update in background -> somtimes it might need to much time to run

            thread = threading.Thread(target=update_workspace_metadata)
            thread.daemon = True
            thread.start()

            container_size_in_gb = get_container_size()

            if MAX_CONTAINER_SIZE:
                if container_size_in_gb > MAX_CONTAINER_SIZE:
                    # Wait for metadata update before showing the warning
                    # sleep 50 ms -> metadata file should have been updated, otherwise use old metadata
                    time.sleep(0.050)
                    container_size_in_gb = get_container_size()
                result["containerSize"] = round(container_size_in_gb, 1)
                result["containerSizeLimit"] = round(MAX_CONTAINER_SIZE)

                if container_size_in_gb > MAX_CONTAINER_SIZE:
                    # Still bigger after update -> show the warning
                    result["containerSizeWarning"] = True
                    log.info(
                        "You have exceeded the limit the container size. Please clean up."
                    )
                else:
                    result["containerSizeWarning"] = False

            workspace_folder_size_in_gb = get_workspace_folder_size()

            if MAX_WORKSPACE_FOLDER_SIZE:
                if workspace_folder_size_in_gb > MAX_WORKSPACE_FOLDER_SIZE:
                    # Wait for metadata update before showing the warning
                    # sleep 50 ms -> metadata file should have been updated, otherwise use old metadata
                    time.sleep(0.050)
                    workspace_folder_size_in_gb = get_workspace_folder_size()
                result["workspaceFolderSize"] = round(workspace_folder_size_in_gb, 1)
                result["workspaceFolderSizeLimit"] = round(MAX_WORKSPACE_FOLDER_SIZE)

                if workspace_folder_size_in_gb > MAX_WORKSPACE_FOLDER_SIZE:
                    # Still bigger after update -> show the warning
                    result["workspaceFolderSizeWarning"] = True
                    log.info(
                        "You have exceeded the limit the workspace folder size. Please clean up."
                    )
                else:
                    result["workspaceFolderSizeWarning"] = False

            self.finish(json.dumps(result))

        except Exception as ex:
            handle_error(self, 500, exception=ex)
            return


# ------------- Storage Check Utils ------------------------


def get_last_usage_date(path):
    """
    Return the most recent usage timestamp for a filesystem path based on modification, access, and creation times.
    
    The function compares the file's modification time, access time, and creation time by calendar date (day precision) and returns the newest corresponding datetime. If the path does not exist or any filesystem errors occur while reading timestamps, the function returns `None`.
    
    Parameters:
        path (str): Filesystem path to check.
    
    Returns:
        datetime or None: A datetime for the latest usage date (mtime/atime/ctime) or `None` if unavailable.
    """
    date = None

    if not os.path.exists(path):
        log.info("Path does not exist: " + path)
        return date

    with contextlib.suppress(Exception):
        date = datetime.fromtimestamp(os.path.getmtime(path))

    with contextlib.suppress(Exception):
        compare_date = datetime.fromtimestamp(os.path.getatime(path))
        if date.date() < compare_date.date():
            # compare date is newer
            date = compare_date

    with contextlib.suppress(Exception):
        compare_date = datetime.fromtimestamp(os.path.getctime(path))
        if date.date() < compare_date.date():
            # compare date is newer
            date = compare_date

    return date


def update_workspace_metadata():
    """
    Update the workspace metadata file with a current timestamp and optional size metrics.
    
    Writes a JSON file named "metadata.json" into WORKSPACE_CONFIG_FOLDER containing:
    - update_timestamp: current datetime string,
    - container_size_in_kb: integer size of the container root in kilobytes or None if not computed,
    - workspace_folder_size_in_kb: integer size of WORKSPACE_HOME in kilobytes or None if not computed.
    
    When MAX_CONTAINER_SIZE is set the function attempts to compute the container size; when MAX_WORKSPACE_FOLDER_SIZE is set it attempts to compute the workspace folder size. Computation failures are suppressed; in that case the corresponding value remains None.
    """
    workspace_metadata = {
        "update_timestamp": str(datetime.now()),
        "container_size_in_kb": None,
        "workspace_folder_size_in_kb": None,
    }

    if MAX_CONTAINER_SIZE:
        # calculate container size via the root folder
        with contextlib.suppress(Exception):
            # exclude all different filesystems/mounts
            workspace_metadata["container_size_in_kb"] = int(
                subprocess.check_output(["du", "-sx", "--exclude=/proc", "/"])
                .split()[0]
                .decode("utf-8")
            )

    if MAX_WORKSPACE_FOLDER_SIZE:
        # calculate workspace folder size
        with contextlib.suppress(Exception):
            # exclude all different filesystems/mounts
            workspace_metadata["workspace_folder_size_in_kb"] = int(
                subprocess.check_output(["du", "-sx", WORKSPACE_HOME])
                .split()[0]
                .decode("utf-8")
            )

    if not os.path.exists(WORKSPACE_CONFIG_FOLDER):
        os.makedirs(WORKSPACE_CONFIG_FOLDER)

    with open(os.path.join(WORKSPACE_CONFIG_FOLDER, "metadata.json"), "w") as file:
        json.dump(workspace_metadata, file, sort_keys=True, indent=4)


def get_workspace_metadata():
    workspace_metadata = {}
    metadata_file_path = os.path.join(WORKSPACE_CONFIG_FOLDER, "metadata.json")
    if os.path.isfile(metadata_file_path):
        try:
            with open(metadata_file_path, "rb") as file:
                workspace_metadata = json.load(file)
        except Exception:
            pass
    return workspace_metadata


def get_container_size():
    try:
        workspace_metadata = get_workspace_metadata()
        return int(workspace_metadata["container_size_in_kb"]) / 1024 / 1024
    except Exception:
        return 0


def get_workspace_folder_size():
    try:
        workspace_metadata = get_workspace_metadata()
        return int(workspace_metadata["workspace_folder_size_in_kb"]) / 1024 / 1024
    except Exception:
        return 0


def get_minutes_since_size_update():
    """
    Return the number of minutes elapsed since the workspace metadata update timestamp.
    
    Reads the "update_timestamp" field from the workspace metadata file (expected format "%Y-%m-%d %H:%M:%S.%f") and returns the elapsed whole minutes truncated to the range 0–59. Returns `None` if the metadata file or timestamp is missing or cannot be parsed.
    
    Returns:
        int: Minutes (0–59) since the last update, or `None` if unavailable or invalid.
    """
    metadata_file_path = os.path.join(WORKSPACE_CONFIG_FOLDER, "metadata.json")
    if os.path.isfile(metadata_file_path):
        try:
            with open(metadata_file_path, "rb") as file:
                workspace_metadata = json.load(file)
                update_timestamp_str = workspace_metadata["update_timestamp"]

                if not update_timestamp_str:
                    return None
                updated_date = datetime.strptime(
                    update_timestamp_str, "%Y-%m-%d %H:%M:%S.%f"
                )
                return ((datetime.now() - updated_date).seconds // 60) % 60
        except Exception:
            return None
    return None


def get_inactive_days():
    # read inactive days from metadata timestamp (update when user is actively using the workspace)
    """
    Return the number of whole days since the workspace's last recorded update.
    
    Reads WORKSPACE_CONFIG_FOLDER/metadata.json and parses the `update_timestamp` (format "%Y-%m-%d %H:%M:%S.%f"). 
    
    Returns:
        inactive_days (int): Whole days elapsed since `update_timestamp`. Returns 0 if the metadata file or timestamp is missing or cannot be parsed.
    """
    metadata_file_path = os.path.join(WORKSPACE_CONFIG_FOLDER, "metadata.json")
    if os.path.isfile(metadata_file_path):
        try:
            with open(metadata_file_path, "rb") as file:
                workspace_metadata = json.load(file)
                update_timestamp_str = workspace_metadata["update_timestamp"]

                if not update_timestamp_str:
                    return 0
                updated_date = datetime.strptime(
                    update_timestamp_str, "%Y-%m-%d %H:%M:%S.%f"
                )
                inactive_days = (datetime.now() - updated_date).days
                return inactive_days
        except Exception:
            return 0
    # return 0 as fallback
    return 0


def cleanup_folder(
    folder_path: str,
    max_file_size_mb: int = 50,
    last_file_usage: int = 3,
    replace_with_info: bool = True,
    excluded_folders: list = None,
):
    """
    Remove large or long-unused files from a folder to free disk space.
    
    Files whose size is greater than or equal to `max_file_size_mb` and whose last usage is older than `last_file_usage` days are removed. After scanning, workspace metadata is updated. Optionally writes a `.removed.txt` next to each removed file containing the removal reason.
    
    Parameters:
        folder_path (str): Root folder to scan and clean.
        max_file_size_mb (int): Threshold size in megabytes; files with size >= this value are eligible for removal. If falsy, size is ignored.
        last_file_usage (int): Minimum number of days since last file usage required for removal.
        replace_with_info (bool): If True, create a `<filename>.removed.txt` containing the removal reason next to each removed file.
        excluded_folders (list[str] | None): Folder names to skip during traversal (matched against immediate subdirectory names).
    """
    total_cleaned_up_mb = 0
    removed_files = 0

    for dirname, subdirs, files in os.walk(folder_path):
        if excluded_folders:
            for excluded_folder in excluded_folders:
                if excluded_folder in subdirs:
                    log.debug("Ignoring folder because of name: " + excluded_folder)
                    subdirs.remove(excluded_folder)
        for filename in files:
            file_path = os.path.join(dirname, filename)

            file_size_mb = int(os.path.getsize(file_path) / (1024.0 * 1024.0))
            if max_file_size_mb and max_file_size_mb > file_size_mb:
                # File will not be deleted since it is less than the max size
                continue

            last_file_usage_days = None
            if get_last_usage_date(file_path):
                last_file_usage_days = (
                    datetime.now() - get_last_usage_date(file_path)
                ).days

            if last_file_usage_days and last_file_usage_days <= last_file_usage:
                continue

            current_date_str = datetime.now().strftime("%B %d, %Y")
            removal_reason = (
                "File has been removed during folder cleaning ("
                + folder_path
                + ") on "
                + current_date_str
                + ". "
            )
            if file_size_mb and max_file_size_mb:
                removal_reason += (
                    "The file size was "
                    + str(file_size_mb)
                    + " MB (max "
                    + str(max_file_size_mb)
                    + "). "
                )

            if last_file_usage_days and last_file_usage:
                removal_reason += (
                    "The last usage was "
                    + str(last_file_usage_days)
                    + " days ago (max "
                    + str(last_file_usage)
                    + "). "
                )

            log.info(filename + ": " + removal_reason)

            # Remove file
            try:
                os.remove(file_path)

                if replace_with_info:
                    with open(file_path + ".removed.txt", "w") as file:
                        file.write(removal_reason)

                if file_size_mb:
                    total_cleaned_up_mb += file_size_mb

                removed_files += 1

            except Exception as e:
                log.info("Failed to remove file: " + file_path, e)

    # check diskspace and update workspace metadata
    update_workspace_metadata()
    log.info(
        "Finished cleaning. Removed "
        + str(removed_files)
        + " files with a total disk space of "
        + str(total_cleaned_up_mb)
        + " MB."
    )


# ------------- GIT FUNCTIONS ------------------------


def execute_command(cmd: str):
    return subprocess.check_output(cmd.split()).decode("utf-8").replace("\n", "")


def get_repo(directory: str):
    if not directory:
        return None

    try:
        return git.Repo(directory, search_parent_directories=True)
    except Exception:
        return None


def set_user_email(email: str, repo=None):
    """
    Set the Git user email for a repository or for the global Git configuration.
    
    If `repo` is provided, write the email to that repository's local Git config; otherwise attempt to set the global Git `user.email`. If setting the global configuration fails, a warning is emitted.
    
    Parameters:
        email (str): Email address to set as the Git user email.
        repo (optional): A git.Repo instance whose local configuration should be updated. If omitted, the global Git configuration is used.
    """
    if repo:
        repo.config_writer().set_value("user", "email", email).release()
    else:
        exit_code = subprocess.call(
            'git config --global user.email "' + email + '"', shell=True
        )
        if exit_code > 0:
            warnings.warn("Global email configuration failed.", stacklevel=2)


def set_user_name(name: str, repo=None):
    """
    Configure the Git user name for a repository or the global Git configuration.
    
    Sets the `user.name` value in the provided repository's local configuration when `repo` is given; otherwise attempts to set the global Git `user.name`. If the global configuration command fails, a runtime warning is emitted.
    
    Parameters:
        name (str): The user name to set for Git commits.
        repo (optional): A git.Repo instance whose local configuration should be updated; if omitted, the global Git configuration is used.
    """
    if repo:
        repo.config_writer().set_value("user", "name", name).release()
    else:
        exit_code = subprocess.call(["git", "config", "--global", "user.name", name])
        if exit_code > 0:
            warnings.warn("Global name configuration failed.", stacklevel=2)


def commit_file(file_path: str, commit_msg: str = None, push: bool = True):
    """
    Stage, commit, and optionally push a single file to its containing git repository.
    
    Parameters:
        file_path (str): Path to the file to commit.
        commit_msg (str, optional): Commit message to use. If omitted, a default message
            "Updated <relative-path>" is used relative to the repository root.
        push (bool, optional): If True, push the commit to the remote named "origin".
    
    Raises:
        Exception: If the file does not exist.
        Exception: If no git repository can be found for the file.
        Exception: If git user.name is not configured for the repository.
        Exception: If git user.email is not configured for the repository.
        Exception: If the repository cannot be fast-forwarded or updated (pull failure).
        Exception: If the file has no changes to commit.
        Exception: If pushing fails due to authentication issues (suggests logging in or using SSH).
        git.GitCommandError: For other git command failures not translated above.
    """
    if not os.path.isfile(file_path):
        raise Exception("File does not exist: " + file_path)

    repo = get_repo(os.path.dirname(file_path))
    if not repo:
        raise Exception("No git repo was found for file: " + file_path)

    # Always add file
    repo.index.add([file_path])

    if not get_user_name(repo):
        raise Exception(
            'Cannot push to remote. Please specify a name with: git config --global user.name "YOUR NAME"'
        )

    if not get_user_email(repo):
        raise Exception(
            'Cannot push to remote. Please specify an email with: git config --global user.emails "YOUR EMAIL"'
        )

    if not commit_msg:
        commit_msg = "Updated " + os.path.relpath(file_path, repo.working_dir)

    try:
        # fetch and merge newest state - fast-forward-only
        repo.git.pull("--ff-only")
    except Exception as ex:
        raise Exception("The repo is not up-to-date or cannot be updated.") from ex

    try:
        # Commit single file with commit message
        repo.git.commit(file_path, m=commit_msg)
    except git.GitCommandError as error:
        if error.stdout and (
            "branch is up-to-date with" in error.stdout
            or "branch is up to date with" in error.stdout
        ):
            # TODO better way to check if file has changed, e.g. has_file_changed
            raise Exception("File has not been changed: " + file_path) from None
        else:
            raise error

    if push:
        # Push file to remote
        try:
            repo.git.push("origin", "HEAD")
        except git.GitCommandError as error:
            if error.stderr and (
                "No such device or address" in error.stderr
                and "could not read Username" in error.stderr
            ):
                raise Exception(
                    "User is not authenticated. Please use Ungit to login via HTTPS or use SSH authentication."
                ) from error
            else:
                raise error


def get_config_value(key: str, repo=None):
    try:
        if repo:
            return repo.git.config(key)
        # no repo, look up global config
        return execute_command("git config " + key)
    except Exception:
        return None


def get_user_name(repo=None):
    return get_config_value("user.name", repo)


def get_user_email(repo=None):
    return get_config_value("user.email", repo)


def get_active_branch(repo) -> str or None:
    try:
        return repo.active_branch.name
    except Exception:
        return None


def get_last_commit(repo) -> str or None:
    try:
        return datetime.fromtimestamp(repo.head.commit.committed_date).strftime(
            "%d.%B %Y %I:%M:%S"
        )
    except Exception:
        return None


def has_file_changed(repo, file_path: str):
    # not working in all situations
    changed_files = [item.a_path for item in repo.index.diff(None)]
    return os.path.relpath(os.path.realpath(file_path), repo.working_dir) in (
        path for path in changed_files
    )


def get_git_info(directory: str):
    repo = get_repo(directory)
    git_info = {
        "userName": get_user_name(repo),
        "userEmail": get_user_email(repo),
        "repoRoot": repo.working_dir if repo else None,
        "activeBranch": get_active_branch(repo) if repo else None,
        "lastCommit": get_last_commit(repo) if repo else None,
        "requestPath": directory,
    }
    return git_info


def _get_server_root() -> str:
    return os.path.expanduser(web_app.settings["server_root_dir"])


def _resolve_path(path: str) -> str or None:
    if path:
        # add jupyter server root directory
        if path.startswith("/"):
            path = path[1:]

        return os.path.join(_get_server_root(), path)
    else:
        return None


# ------------- SSH Functions ------------------------
def handle_ssh_script_request(handler):
    origin = handler.get_argument("origin", None)
    host = handler.get_argument("host", None)
    port = handler.get_argument("port", None)

    if not host and origin:
        host, _ = parse_endpoint_origin(origin)

    if not port and origin:
        _, port = parse_endpoint_origin(origin)

    if not host:
        handle_error(
            handler,
            400,
            "Please provide a host via get parameter. Alternatively, you can also specify an origin with the full endpoint url.",
        )
        return

    if not port:
        handle_error(
            handler,
            400,
            "Please provide a port via get parameter. Alternatively, you can also specify an origin with the full endpoint url.",
        )
        return

    setup_script = get_setup_script(host, port)

    download_script_flag = handler.get_argument("download", None)
    if download_script_flag and download_script_flag.lower().strip() == "true":
        # Use host, otherwise it cannot be reconstructed in tooling plugin

        file_name = "setup_ssh_{}-{}".format(host.lower().replace(".", "-"), port)
        SSH_JUMPHOST_TARGET = os.environ.get("SSH_JUMPHOST_TARGET", "")
        if SSH_JUMPHOST_TARGET:
            # add name if variabl is set
            file_name += "-" + SSH_JUMPHOST_TARGET.lower().replace(".", "-")
        file_name += ".sh"

        handler.set_header("Content-Type", "application/octet-stream")
        handler.set_header(
            "Content-Disposition", "attachment; filename=" + file_name
        )  # Hostname runtime
        handler.write(setup_script)
        handler.finish()
    else:
        handler.finish(setup_script)


def parse_endpoint_origin(endpoint_url: str):
    # get host and port from endpoint url
    from urllib.parse import urlparse

    endpoint_url = urlparse(endpoint_url)
    hostname = endpoint_url.hostname
    port = endpoint_url.port
    if not port:
        port = 80
        if endpoint_url.scheme == "https":
            port = 443
    return hostname, str(port)


def generate_token(base_url: str):
    private_ssh_key_path = HOME + "/.ssh/id_ed25519"
    with open(private_ssh_key_path, "r") as f:
        runtime_private_key = f.read()

    import hashlib

    key_hasher = hashlib.sha1()
    key_hasher.update(str.encode(str(runtime_private_key).lower().strip()))
    key_hash = key_hasher.hexdigest()

    token_hasher = hashlib.sha1()
    token_str = (key_hash + base_url).lower().strip()
    token_hasher.update(str.encode(token_str))
    return str(token_hasher.hexdigest())


def get_setup_script(hostname: str = None, port: str = None):
    """
    Builds the client SSH setup script by embedding the runtime private key, host/port values, keyscan entry, and a generated runtime configuration name.
    
    Parameters:
        hostname (str | None): Hostname to use for the runtime or, when SSH_JUMPHOST_TARGET is set, the manager host. If None, placeholders in the template may remain.
        port (str | None): Port to use for the runtime or, when SSH_JUMPHOST_TARGET is set, the manager port.
    
    Returns:
        str: The rendered setup script with all template placeholders substituted (private key, hostname/port, known-hosts entry, and runtime config name).
    """
    private_ssh_key_path = HOME + "/.ssh/id_ed25519"
    with open(private_ssh_key_path, "r") as f:
        runtime_private_key = f.read()

    ssh_templates_path = os.path.dirname(os.path.abspath(__file__)) + "/setup_templates"

    with open(ssh_templates_path + "/client_command.txt", "r") as file:
        client_command = file.read()

    SSH_JUMPHOST_TARGET = os.environ.get("SSH_JUMPHOST_TARGET", "")
    is_runtime_manager_existing = bool(SSH_JUMPHOST_TARGET)

    RUNTIME_CONFIG_NAME = "workspace-"
    if is_runtime_manager_existing:
        HOSTNAME_RUNTIME = SSH_JUMPHOST_TARGET
        HOSTNAME_MANAGER = hostname
        PORT_MANAGER = port
        PORT_RUNTIME = os.getenv("WORKSPACE_PORT", "8080")

        RUNTIME_CONFIG_NAME = RUNTIME_CONFIG_NAME + "{}-{}-{}".format(
            HOSTNAME_RUNTIME, HOSTNAME_MANAGER, PORT_MANAGER
        )

        client_command = (
            client_command.replace("{HOSTNAME_MANAGER}", HOSTNAME_MANAGER)
            .replace("{PORT_MANAGER}", str(PORT_MANAGER))
            .replace("#ProxyCommand", "ProxyCommand")
        )

        local_keyscan_replacement = "{}".format(HOSTNAME_RUNTIME)
    else:
        HOSTNAME_RUNTIME = hostname
        PORT_RUNTIME = port
        RUNTIME_CONFIG_NAME = RUNTIME_CONFIG_NAME + "{}-{}".format(
            HOSTNAME_RUNTIME, PORT_RUNTIME
        )

        local_keyscan_replacement = "[{}]:{}".format(HOSTNAME_RUNTIME, PORT_RUNTIME)

    # perform keyscan with localhost to get the runtime's keyscan result.
    # Replace then the "localhost" part in the returning string with the actual RUNTIME_HOST_NAME
    local_keyscan_entry = get_ssh_keyscan_results("localhost")
    if local_keyscan_entry is not None:
        local_keyscan_entry = local_keyscan_entry.replace(
            "localhost", local_keyscan_replacement
        )

    output = (
        client_command.replace("{PRIVATE_KEY_RUNTIME}", runtime_private_key)
        .replace("{HOSTNAME_RUNTIME}", HOSTNAME_RUNTIME)
        .replace("{RUNTIME_KNOWN_HOST_ENTRY}", local_keyscan_entry)
        .replace("{PORT_RUNTIME}", str(PORT_RUNTIME))
        .replace("{RUNTIME_CONFIG_NAME}", RUNTIME_CONFIG_NAME)
        .replace(
            "{RUNTIME_KEYSCAN_NAME}",
            local_keyscan_replacement.replace("[", r"\[").replace("]", r"\]"),
        )
    )

    return output


def get_ssh_keyscan_results(host_name, host_port=22, key_format="ecdsa"):
    """
    Perform the keyscan command to get the certicicate fingerprint (of specified format [e.g. rsa256, ecdsa, ...]) of the container.

    # Arguments
      - host_name (string): hostname which to scan for a key
      - host_port (int): port which to scan for a key
      - key_format (string): type of the key to return. the `ssh-keyscan` command usually lists the fingerprint in different formats (e.g. ecdsa-sha2-nistp256, ssh-rsa, ssh-ed25519, ...). The ssh-keyscan result is grepped for the key_format, so already a part could match. In that case, the last match is used.

    # Returns
      The keyscan entry which can be added to the known_hosts file. If `key_format` matches multiple results of `ssh-keyscan`, the last match is returned. If no match exists, it returns empty
    """

    keyscan_result = subprocess.run(
        ["ssh-keyscan", "-p", str(host_port), host_name], stdout=subprocess.PIPE
    )
    keys = keyscan_result.stdout.decode("utf-8").split("\n")
    keyscan_entry = ""
    for key in keys:
        if key_format in key:
            keyscan_entry = key
    return keyscan_entry


# ------------- PLUGIN LOADER ------------------------


def load_jupyter_server_extension(nb_server_app) -> None:
    # registers all handlers as a REST interface
    global web_app
    global log

    web_app = nb_server_app.web_app
    log = nb_server_app.log

    host_pattern = ".*$"

    # SharedSSHHandler

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/ping")
    web_app.add_handlers(host_pattern, [(route_pattern, PingHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/tools")
    web_app.add_handlers(host_pattern, [(route_pattern, ToolingHandler)])

    route_pattern = url_path_join(
        web_app.settings["base_url"], "/tooling/tool-installers"
    )
    web_app.add_handlers(host_pattern, [(route_pattern, InstallToolHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/token")
    web_app.add_handlers(host_pattern, [(route_pattern, SharedTokenHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/git/info")
    web_app.add_handlers(host_pattern, [(route_pattern, GitInfoHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/git/commit")
    web_app.add_handlers(host_pattern, [(route_pattern, GitCommitHandler)])

    web_app.add_handlers(
        host_pattern,
        [
            (
                url_path_join(web_app.settings["base_url"], "/tooling/storage/check"),
                StorageCheckHandler,
            )
        ],
    )

    route_pattern = url_path_join(
        web_app.settings["base_url"], "/tooling/ssh/setup-script"
    )
    web_app.add_handlers(host_pattern, [(route_pattern, SSHScriptHandler)])

    route_pattern = url_path_join(
        web_app.settings["base_url"], "/tooling/ssh/setup-command"
    )
    web_app.add_handlers(host_pattern, [(route_pattern, SSHCommandHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], "/tooling/files/link")
    web_app.add_handlers(host_pattern, [(route_pattern, SharedFilesHandler)])

    route_pattern = url_path_join(web_app.settings["base_url"], SHARED_SSH_SETUP_PATH)
    web_app.add_handlers(host_pattern, [(route_pattern, SharedSSHHandler)])

    log.info("Extension jupyter-tooling-widget loaded successfully.")


# Test routine. Can be invoked manually
if __name__ == "__main__":
    application = tornado.web.Application([(r"/test", HelloWorldHandler)])

    application.listen(555)
    tornado.ioloop.IOLoop.current().start()