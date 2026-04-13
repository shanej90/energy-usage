############################################
# FUNCTIONS TO MAKE IT EASIER TO DEAL WITH PROJECT DIRECTORIES
################################################

#imports
import os

#identify project root
def find_project_root(marker_file = "readme.md"):
    
    """
    Recursively searches parent directories to locate the project root based on a specified marker file.

    This function starts from the directory of the current script and moves upward through parent 
    directories until it finds one that contains the specified marker file. It is useful for ensuring 
    consistent, root-relative paths in modular Python projects.

    Parameters:
        marker_folder (str): The name of a file that signifies the project root 
                             (eg "readme.md"). Defaults to "readme.md".

    Returns:
        str: Absolute path to the project root directory containing the marker file.

    Raises:
        RuntimeError: If no directory containing the marker file is found before reaching the filesystem root.
    """
    
    path = os.path.abspath(__file__)
    
    while True:
        path = os.path.dirname(path)
        if os.path.exists(os.path.join(path, marker_file)):
            return path
        if path == os.path.dirname(path):  # Reached root of filesystem
            raise RuntimeError("Project root not found. Please ensure specified marker file exists.")