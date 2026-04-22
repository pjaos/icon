#!/usr/bin/env python3

import argparse

from p3lib.uio import UIO
from p3lib.helper import logTraceBack
from p3lib.boot_manager import BootManager
from p3lib.helper import get_app_data_path
from p3lib.helper import get_program_version

MODULE_NAME = "icon"

class IConDB(object):
    PROGRAM_NAME = "icon"

    def __init__(self, uio, options):
        """@brief Constructor
           @param uio A UIO instance handling user input and output (E.G stdin/stdout or a GUI)
           @param options An instance of the OptionParser command line options."""
        self._uio = uio
        self._options = options
        self._config_folder = get_app_data_path(MODULE_NAME) # All configuration and app data sits in this folder.

    def run(self):
        # Implementation required here
        pass


def main():
    """@brief Program entry point"""
    uio = UIO()
    prog_version = get_program_version(IConDB.PROGRAM_NAME)
    uio.info(f"{IConDB.PROGRAM_NAME}: V{prog_version}")

    try:
        parser = argparse.ArgumentParser(description="A tool that repeatedly performs checks on internet connectivity and stores the data to a local sqllite database.",
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("-d", "--debug",  action='store_true', help="Enable debugging.")
        parser.add_argument("-t", "--host",   help="The host address that traceroute will use to check internet connectivity.", default=None, required=False)
        parser.add_argument("-p", "--poll_seconds",    type=float, help="A periodicity of the traceroute command execution, in seconds.")
        # Add args to auto boot cmd
        BootManager.AddCmdArgs(parser)

        options = parser.parse_args()

        uio.enableDebug(options.debug)

        handled = BootManager.HandleOptions(uio, options, False)
        if not handled:
            aClass = IConDB(uio, options)
            aClass.run()

    # If the program throws a system exit exception
    except SystemExit:
        pass
    # Don't print error information if CTRL C pressed
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        logTraceBack(uio)

        if options.debug:
            raise
        else:
            uio.error(str(ex))


if __name__ == '__main__':
    main()
