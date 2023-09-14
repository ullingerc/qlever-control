#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

# This is the QLever script (new version, written in Python).

from configparser import ConfigParser, ExtendedInterpolation
from datetime import datetime, date
import os
import glob
import inspect
import logging
import psutil
import re
import shlex
import subprocess
import sys
import time
from termcolor import colored
import traceback

BLUE = "\033[34m"
RED = "\033[31m"
BOLD = "\033[1m"
NORMAL = "\033[0m"


# Custom formatter for log messages.
class CustomFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        if record.levelno == logging.DEBUG:
            return colored(message, "magenta")
        elif record.levelno == logging.WARNING:
            return colored(message, "yellow")
        elif record.levelno in [logging.CRITICAL, logging.ERROR]:
            return colored(message, "red")
        else:
            return message


# Custom logger.
log = logging.getLogger("qlever")
log.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(CustomFormatter())
log.addHandler(handler)


# Helper function for tracking the order of the actions in class `Actions`.
def track_action_rank(method):
    method.rank = track_action_rank.counter
    track_action_rank.counter += 1
    return method
track_action_rank.counter = 0  # noqa: E305


class ActionException(Exception):
    pass


class Actions:

    def __init__(self):
        self.config = ConfigParser(interpolation=ExtendedInterpolation())
        self.config.read("Qleverfile.NEW")
        self.name = self.config['DEFAULT']['name']
        self.yes_values = ["1", "true", "yes"]

        # Default values for options that are not mandatory in the Qleverfile.
        defaults = {
            "general": {
                "log_level": "info",
            },
            "server": {
                "binary": "ServerMain",
                "num_threads": "8",
                "cache_max_size_gb": "5",
                "cache_max_size_gb_single_entry": "1",
                "cache_max_num_entries": "100",
                "with_text_index": "no",
                "only_pso_and_pos_permutations": "no",
                "no_patterns": "no",
            },
            "index": {
                "binary": "IndexBuilderMain",
                "with_text_index": "no",
                "only_pso_and_pos_permutations": "no",
                "no_patterns": "no",
            },
            "docker": {
                "image": "adfreiburg/qlever",
                "container_server": f"qlever.server.{self.name}",
                "container_indexer": f"qlever.indexer.{self.name}",
            },
            "ui": {
                "port": "7000",
            }
        }
        for section in defaults:
            # If the section does not exist, create it.
            if not self.config.has_section(section):
                self.config[section] = {}
            # If an option does not exist, set it to the default value.
            for option in defaults[section]:
                if not self.config[section].get(option):
                    self.config[section][option] = defaults[section][option]

        # If the log level was not explicitly set by the first command-line
        # argument (see below), set it according to the Qleverfile.
        if log.level == logging.NOTSET:
            log_level = self.config['general']['log_level'].upper()
            try:
                log.setLevel(getattr(logging, log_level))
            except AttributeError:
                log.error(f"Invalid log level: \"{log_level}\"")
                sys.exit(1)

        # Show some information (for testing purposes only).
        log.debug(f"Parsed Qleverfile, sections are: "
                  f"{', '.join(self.config.sections())}")

        # Check specifics of the installation.
        self.check_installation()

    def check_installation(self):
        """
        Helper function that checks particulars of the installation and
        remembers them so that all actions execute without errors.
        """

        # Handle the case Systems like macOS do not allow
        # psutil.net_connections().
        try:
            psutil.net_connections()
            self.net_connections_enabled = True
        except Exception as e:
            self.net_connections_enabled = False
            log.debug(f"Note: psutil.net_connections() failed ({e}),"
                      f" will not scan network connections for action"
                      f" \"start\"")

        # Check whether docker is installed and works (on MacOS 12, docker
        # hangs when installed without GUI, hence the timeout).
        try:
            completed_process = subprocess.run(
                    ["docker", "info"], timeout=0.5,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if completed_process.returncode != 0:
                raise Exception("docker info failed")
            self.docker_enabled = True
        except Exception:
            self.docker_enabled = False
            print("Note: `docker info` failed, therefore"
                  " docker.USE_DOCKER=true not supported")

        # Check if the QLever binaries work.
        try:
            subprocess.run([self.config['server']['binary'], "--help"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            subprocess.run([self.config['index']['binary'], "--help"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self.binaries_work = True
        except Exception:
            print("Note: QLever binaries not found or failed, therefore"
                  " docker.USE_DOCKER=false not supported")
            self.binaries_work = False

    def set_config(self, section, option, value):
        """
        Helper function that sets a value in the config file (and throws an
        exceptionon if the section or option does not exist).
        """

        if not self.config.has_section(section):
            log.error(f"Section [{section}] does not exist in Qleverfile")
            sys.exit(1)
        if not self.config.has_option(section, option):
            log.error(f"Option {option.upper()} does not exist in section "
                      f"[{section}] in Qleverfile")
            sys.exit(1)
        self.config[section][option] = value

    def get_total_file_size(self, paths):
        """
        Helper function that gets the total size of all files in the given
        paths in GB.
        """

        total_size = 0
        for path in paths:
            for file in glob.glob(path):
                total_size += os.path.getsize(file)
        return total_size / 1e9

    def alive_check(self, port):
        """
        Helper function that checks if a QLever server is running on the given
        port.
        """

        message = "from the qlever script".replace(" ", "%20")
        curl_cmd = f"curl -s http://localhost:{port}/ping?msg={message}"
        exit_code = subprocess.call(curl_cmd, shell=True,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        return exit_code == 0

    @track_action_rank
    def action_show_config(self, only_show=False):
        """
        Action that shows the current configuration including the default
        values for options that are not set explicitly in the Qleverfile.
        """

        print(f"{BLUE}Showing the current configuration, including default"
              f" values for options that are not set explicitly in the"
              f" Qleverfile{NORMAL}")
        for section in ['DEFAULT'] + self.config.sections():
            print()
            print(f"[{section}]")
            max_option_length = max([len(option) for option in
                                     self.config[section]])
            for option in self.config[section]:
                if section == "DEFAULT" or \
                        option not in self.config['DEFAULT']:
                    print(f"{option.upper().ljust(max_option_length)} = "
                          f"{self.config[section][option]}")

        print()

    @track_action_rank
    def action_get_data(self, only_show=False):
        """
        Action that gets the data according to GET_DATA_CMD.
        """

        if not self.config['data']['get_data_cmd']:
            print(f"{RED}No GET_DATA_CMD specified in Qleverfile")
            return
        cmdline = self.config['data']['get_data_cmd']
        print(f"{BLUE}{cmdline}{NORMAL}")
        if not only_show:
            print()
            os.system(cmdline)
            total_file_size = self.get_total_file_size(
                self.config['index']['file_names'].split())
            print(f"Total file size: {total_file_size:.1f} GB")
            # os.system(f"ls -lh {self.config['index']['file_names']}")

    @track_action_rank
    def action_index(self, only_show=False):
        """
        Action that builds a QLever index according to the settings in the
        [index] section of the Qleverfile.
        """

        # Write settings.json file.
        with open(f"{self.name}.settings.json", "w") as f:
            f.write(self.config['index']['settings_json'])

        # Construct the command line based on the config file.
        index_config = self.config['index']
        cmdline = (f"{index_config['cat_files']} | {index_config['binary']}"
                   f" -F ttl -f -"
                   f" -i {self.name}"
                   f" -s {self.name}.settings.json")
        if index_config['only_pso_and_pos_permutations'] in self.yes_values:
            cmdline += " --only-pso-and-pos-permutations --no-patterns"
        if index_config['with_text_index'] in \
                ["from_text_records", "from_text_records_and_literals"]:
            cmdline += (f" -w {self.name}.wordsfile.tsv"
                        f" -d {self.name}.docsfile.tsv")
        if index_config['with_text_index'] in \
                ["from_literals", "from_text_records_and_literals"]:
            cmdline += " --text-words-from-literals"
        if 'stxxl_memory_gb' in index_config:
            cmdline += f" --stxxl-memory-gb {index_config['stxxl_memory_gb']}"
        cmdline += f" | tee {self.name}.index-log.txt"

        # If the total file size is larger than 10 GB, set ulimit (such that a
        # large number of open files is allowed).
        total_file_size = self.get_total_file_size(
                self.config['index']['file_names'].split())
        if total_file_size > 10:
            cmdline = f"ulimit -Sn 1048576; {cmdline}"

        # If we are using Docker, run the command in a Docker container.
        # Here is how the shell script does it:
        if self.config['docker']['use_docker'] in self.yes_values:
            docker_config = self.config['docker']
            cmdline = (f"docker run -it --rm -u $(id -u):$(id -g)"
                       f" -v /etc/localtime:/etc/localtime:ro"
                       f" -v $(pwd):/index -w /index"
                       f" --entrypoint bash"
                       f" --name {docker_config['container_indexer']}"
                       f" {docker_config['image']}"
                       f" -c {shlex.quote(cmdline)}")

        # Show the command line.
        print(f"{BLUE}{cmdline}{NORMAL}")
        if only_show:
            return
        print()

        # Check if index files (name.index.*) already exist.
        if glob.glob(f"{self.name}.index.*"):
            raise ActionException(
                    f"Index files for dataset {self.name} already exist, "
                    f"please delete them if you want to rebuild the index")

        # Run the command.
        subprocess.run(cmdline, shell=True)
        # print(f"Return code: {process_completed.returncode}")

    @track_action_rank
    def action_start(self, only_show=False):
        """
        Action that starts the QLever server according to the settings in the
        [server] section of the Qleverfile. If a server is already running, the
        action reports that fact and does nothing.
        """

        # Construct the command line based on the config file.
        server_config = self.config['server']
        cmdline = (f"{self.config['server']['binary']}"
                   f" -i {self.name}"
                   f" -j {server_config['num_threads']}"
                   f" -p {server_config['port']}"
                   f" -m {server_config['memory_for_queries_gb']}"
                   f" -c {server_config['cache_max_size_gb']}"
                   f" -e {server_config['cache_max_size_gb_single_entry']}"
                   f" -k {server_config['cache_max_num_entries']}")
        if server_config['access_token']:
            cmdline += f" -a {server_config['access_token']}"
        if server_config['only_pso_and_pos_permutations'] in self.yes_values:
            cmdline += " --only-pso-and-pos-permutations"
        if server_config['no_patterns'] in self.yes_values:
            cmdline += " --no-patterns"
        if server_config["with_text_index"] in \
                ["from_text_records",
                 "from_literals",
                 "from_text_records_and_literals"]:
            cmdline += " -t"
        cmdline += f" > {self.name}.server-log.txt 2>&1"

        # If we are using Docker, run the command in a docker container.
        if self.config['docker']['use_docker'] in self.yes_values:
            docker_config = self.config['docker']
            cmdline = (f"docker run -d --restart=unless-stopped"
                       f" -u $(id -u):$(id -g)"
                       f" -it -v /etc/localtime:/etc/localtime:ro"
                       f" -v $(pwd):/index"
                       f" -p {server_config['port']}:{server_config['port']}"
                       f" -w /index"
                       f" --entrypoint bash"
                       f" --name {docker_config['container_server']}"
                       f" {docker_config['image']}"
                       f" -c {shlex.quote(cmdline)}")
        else:
            cmdline = f"nohup {cmdline} &"

        # Show the command line (and exit if only_show is True).
        print(f"{BLUE}{cmdline}{NORMAL}")
        if only_show:
            return
        print()

        # Check if a QLever server is already running on this port.
        port = server_config['port']
        if self.alive_check(port):
            raise ActionException(
                    f"QLever server already running on port {port}")

        # Check if another process is already listening.
        if self.net_connections_enabled:
            if port in [conn.laddr.port for conn
                        in psutil.net_connections()]:
                raise ActionException(
                        f"Port {port} is already in use by another process")

        # Execute the command line.
        os.system(cmdline)

        # Tail the server log until the server is ready (note that the `exec`
        # is important to make sure that the tail process is killed and not
        # just the bash process).
        print(f"Follow {self.name}.server-log.txt until the server is ready"
              f" (Ctrl-C stops following the log, but not the server)")
        print()
        tail_cmd = f"exec tail -f {self.name}.server-log.txt"
        tail_proc = subprocess.Popen(tail_cmd, shell=True)
        while not self.alive_check(port):
            time.sleep(1)

        # Set the access token if specified.
        access_token = server_config['access_token']
        access_arg = f"--data-urlencode \"access-token={access_token}\""
        if "index_description" in self.config['data']:
            desc = self.config['data']['index_description']
            curl_cmd = (f"curl -Gs http://localhost:{port}/api"
                        f" --data-urlencode \"index-description={desc}\""
                        f" {access_arg} > /dev/null")
            os.system(curl_cmd)
        if "text_description" in self.config['data']:
            desc = self.config['data']['text_description']
            curl_cmd = (f"curl -Gs http://localhost:{port}/api"
                        f" --data-urlencode \"text-description={desc}\""
                        f" {access_arg} > /dev/null")
            os.system(curl_cmd)

        # Kill the tail process. Note: tail_proc.kill() does not work.
        tail_proc.terminate()

    @track_action_rank
    def action_stop(self, only_show=False):
        """
        Action that stops the QLever server according to the settings in the
        [server] section of the Qleverfile. If no server is running, the action
        does nothing.
        """

        docker_container_name = self.config['docker']['container_server']
        cmdline_regex = (f"{self.config['server']['binary']}\\S+"
                         f" -i [^ ]*{self.name}")
        print(f"{BLUE}Checking for Docker container with name "
              f"\"{docker_container_name}\" and for processes "
              f"matching: {cmdline_regex}{NORMAL}")
        if only_show:
            return
        print()

        # First check if there is docker container running.
        if self.docker_enabled:
            docker_cmd = (f"docker stop {docker_container_name} && "
                          f"docker rm {docker_container_name}")
            try:
                subprocess.run(docker_cmd, shell=True, check=True,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                print(f"Docker container with name "
                      f"\"{docker_container_name}\" "
                      f"stopped and removed")
                return
            except Exception as e:
                log.debug(f"Error running \"{docker_cmd}\": {e}")

        # Check if there is a process running on the server port using psutil.
        #
        # NOTE: On MacOS, some of the proc's returned by psutil.process_iter()
        # no longer exist when we try to access them, so we just skip them.
        for proc in psutil.process_iter():
            try:
                pinfo = proc.as_dict(
                        attrs=['pid', 'username', 'create_time',
                               'memory_info', 'cmdline'])
                cmdline = " ".join(pinfo['cmdline'])
            except Exception as err:
                log.debug(f"Error getting process info: {err}")
            if re.match(cmdline_regex, cmdline):
                print(f"Found process {pinfo['pid']} from user "
                      f"{pinfo['username']} with command line: {cmdline}")
                print()
                try:
                    proc.kill()
                    print(f"{RED}Killed process {pinfo['pid']}{NORMAL}")
                except Exception as e:
                    raise ActionException(
                            f"Could not kill process with PID "
                            f"{pinfo['pid']}: {e}")
                return

        # No matching process found.
        raise ActionException("No matching Docker container or process found")

    @track_action_rank
    def action_status(self, only_show=False):
        """
        Action that shows all QLever processes running on this machine.

        TODO: Also show the QLever-related docker containers.
        """

        cmdline_regex = "^(ServerMain|IndexBuilderMain)"
        print(f"{BLUE}All processes on this machine where "
              f"the command line matches {cmdline_regex}"
              f" using Python's psutil library{NORMAL}")
        print()
        if only_show:
            print(f"{BLUE}If executed, show processes using psutil{NORMAL}")
            return

        # Print the table headers
        num_processes_found = 0
        for proc in psutil.process_iter():
            try:
                pinfo = proc.as_dict(attrs=['pid', 'username', 'create_time',
                                            'memory_info', 'cmdline'])
                cmdline = " ".join(pinfo['cmdline'])
            except Exception:
                continue
            if not re.match(cmdline_regex, cmdline):
                continue
            if num_processes_found == 0:
                print("{:<8} {:<8} {:>5}  {:>5}  {}".format(
                    "PID", "USER", "START", "RSS", "COMMAND"))
            num_processes_found += 1
            pid = pinfo['pid']
            user = pinfo['username'] if pinfo['username'] else ""
            start_time = datetime.fromtimestamp(pinfo['create_time'])
            if start_time.date() == date.today():
                start_time = start_time.strftime("%H:%M")
            else:
                start_time = start_time.strftime("%b%d")
            rss = f"{pinfo['memory_info'].rss / 1e9:.0f} G"
            print("{:<8} {:<8} {:>5}  {:>5}  {}".format(
                pid, user, start_time, rss, cmdline))
        if num_processes_found == 0:
            print("No processes found")


def setup_autocompletion_cmd():
    """
    Print the command for setting up autocompletion for the qlever.py script.

    TODO: Currently work for bash only.
    """

    # Get methods that start wth "action_" from the Actions class, sorted by
    # their appearance in the class (see the `@track_action_rank` decorator).
    methods = inspect.getmembers(Actions, predicate=inspect.isfunction)
    methods = [m for m in methods if m[0].startswith("action_")]
    action_names = sorted([m[0] for m in methods],
                          key=lambda m: getattr(Actions, m).rank)
    action_names = [_.replace("action_", "") for _ in action_names]
    action_names = [_.replace("_", "-") for _ in action_names]
    action_names = " ".join(action_names)

    # Add config settings to the list of possible actions for autocompletion.
    action_names += " docker.USE_DOCKER=true docker.USE_DOCKER=false"
    action_names += " index.BINARY=IndexBuilderMain"
    action_names += " server.BINARY=ServerMain"

    # Return multiline string with the command for setting up autocompletion.
    return f"""\
_qlever_completion() {{
  local cur=${{COMP_WORDS[COMP_CWORD]}}
  COMPREPLY=( $(compgen -W "{action_names}" -- $cur) )
}}
complete -o nosort -F _qlever_completion qlever.py
"""


def main():
    # If there is only argument `setup-autocompletion`, call the function
    # `Actions.setup_autocompletion()` above and exit.
    if len(sys.argv) == 2 and sys.argv[1] == "setup-autocompletion":
        log.setLevel(logging.ERROR)
        print(setup_autocompletion_cmd())
        sys.exit(0)

    # If the first argument sets the log level, deal with that immediately (so
    # that it goes into effect before we do anything else). Otherwise, set the
    # log level to `NOTSET` (which will signal to the Actions class that it can
    # take the log level from the config file).
    log.setLevel(logging.NOTSET)
    if len(sys.argv) > 1:
        set_log_level_match = re.match(r"general.log_level=(\w+)", sys.argv[1])
        if set_log_level_match:
            log_level = set_log_level_match.group(1).upper()
            sys.argv = sys.argv[1:]
            try:
                log.setLevel(getattr(logging, log_level))
                log.debug("")
                log.debug(f"Log level set to {log_level}")
                log.debug("")
            except AttributeError:
                log.error(f"Invalid log level: \"{log_level}\"")
                sys.exit(1)

    # Initalize actions.
    action_names = [_ for _ in dir(Actions) if _.startswith("action_")]
    action_names = [_.replace("action_", "") for _ in action_names]
    action_names = [_.replace("_", "-") for _ in action_names]
    actions = Actions()
    # log.info(f"Actions available are: {', '.join(action_names)}")
    # Show the log level as string.
    # log.info(f"Log level: {logging.getLevelName(log.getEffectiveLevel())}")

    # Check if the last argument is "show" (if yes, remember it and remove it).
    only_show = True if len(sys.argv) > 1 and sys.argv[-1] == "show" else False
    if only_show:
        sys.argv = sys.argv[:-1]

    # Execute the actions specified on the command line.
    for action_name in sys.argv[1:]:
        # If the action is of the form section.key=value, set the config value.
        set_config_match = re.match(r"(\w+)\.(\w+)=(.*)", action_name)
        if set_config_match:
            section, option, value = set_config_match.groups()
            log.info(f"Setting config value: {section}.{option}={value}")
            try:
                actions.set_config(section, option, value)
            except ValueError as err:
                log.error(err)
                sys.exit(1)
            continue
        # If the action name does not exist, exit.
        if action_name not in action_names:
            log.error(f"Action \"{action_name}\" does not exist, available "
                      f"actions are: {', '.join(action_names)}")
            sys.exit(1)
        # Execute the action (or only show what would be executed).
        log.info("")
        log.info(f"{BOLD}Action: \"{action_name}\"{NORMAL}")
        log.info("")
        action = f"action_{action_name.replace('-', '_')}"
        try:
            getattr(actions, action)(only_show=only_show)
        except ActionException as err:
            # line = traceback.extract_tb(err.__traceback__)[-1].lineno
            print(f"{RED}{err}{NORMAL}")
            print()
            sys.exit(1)
        except Exception as err:
            line = traceback.extract_tb(err.__traceback__)[-1].lineno
            print(f"{RED}Error in Python script (line {line}: {err})"
                  f", stack trace follows:{NORMAL}")
            print()
            raise err
    log.info("")


if __name__ == "__main__":
    main()
