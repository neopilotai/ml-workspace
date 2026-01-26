#!/usr/bin/python

"""
Test execute code functionality
"""

import logging
import os
import subprocess
import sys

logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s : %(levelname)s : %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


# Wrapper to print out command
def call(command):
    """
    Execute a shell command and return its exit status.
    
    Prints the command to standard output before execution.
    
    Parameters:
        command (str): Shell command to execute.
    
    Returns:
        int: Exit status code from the executed command.
    """
    print("Executing: " + command)
    return subprocess.call(command, shell=True)


ENV_RESOURCES_PATH = os.getenv("RESOURCES_PATH", "/resources")

exit_code = call(
    ENV_RESOURCES_PATH
    + "/scripts/execute_code.py "
    + ENV_RESOURCES_PATH
    + "/tests/ml-job/"
)

if exit_code == 0:
    print("Code execution test successfull.")
else:
    print("Code execution test failed.")