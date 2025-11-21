"""Debug check to see Python paths in Infrahub runtime."""

import os
import sys
from typing import Any

from infrahub_sdk.checks import InfrahubCheck


class DebugPaths(InfrahubCheck):
    """Check to debug Python paths and directory structure."""

    query = "dc_validation"

    def validate(self, data: Any) -> None:
        """Log Python paths and current directory."""
        
        # Log current working directory
        cwd = os.getcwd()
        self.log_error(message=f"Current working directory: {cwd}")
        
        # Log file location
        file_path = os.path.abspath(__file__)
        self.log_error(message=f"This file location: {file_path}")
        
        # Log parent directory
        parent_dir = os.path.dirname(os.path.dirname(file_path))
        self.log_error(message=f"Parent directory: {parent_dir}")
        
        # Log Python path
        for i, path in enumerate(sys.path):
            self.log_error(message=f"sys.path[{i}]: {path}")
        
        # Check if utils directory exists
        utils_path = os.path.join(parent_dir, "utils")
        utils_exists = os.path.exists(utils_path)
        self.log_error(message=f"Utils directory exists at {utils_path}: {utils_exists}")
        
        if utils_exists:
            utils_contents = os.listdir(utils_path)
            self.log_error(message=f"Utils contents: {utils_contents}")
