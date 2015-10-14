# coding=utf-8
import serial
import os
from threading import Thread, Event, Lock
import imp

from mecode import G
from mecode.printer import Printer
import octoprint.plugin
import flask

from .future_serial import FutureSerial


__author__ = "Jack Minardi <jack@voxel8.co>"
__copyright__ = "Copyright (C) 2015 Voxel8, Inc."


__plugin_name__ = "Automation Scripts"
__plugin_version__ = "0.2.0"
__plugin_author__ = "Jack Minardi"
__plugin_description__ = "Easily run a mecode script from OctoPrint"


SCRIPT_DIR = os.path.expanduser('~/.mecodescripts')


class MecodePlugin(octoprint.plugin.EventHandlerPlugin,
                          octoprint.plugin.SettingsPlugin,
                          octoprint.plugin.TemplatePlugin,
                          octoprint.plugin.AssetPlugin,
                          octoprint.plugin.SimpleApiPlugin,
                          octoprint.plugin.ShutdownPlugin,
                          ):

    def __init__(self):
        if not os.path.exists(SCRIPT_DIR):
            self._logger.warn('Script directory does not exist')
            return

        self.s = None
        self.future_serial = FutureSerial()
        self._read_thread = None
        self._write_thread = None
        self.running = False
        self.event = Event()
        self.event.set()
        self._write_buffer = []
        self._fake_ok = False
        self._temp_resp_len = 0
        self.g = None
        self.read_lock = Lock()
        self.write_lock = Lock()
        self.so = None

        self.scripts = {}
        self.script_titles = {}
        self.script_settings = {}
        self.script_commands = {}
        scriptdir = SCRIPT_DIR
        for filename in [f for f in os.listdir(scriptdir) if f.endswith('.py')]:
            path = os.path.join(scriptdir, filename)
            script = imp.load_source('mecodescript', path)
            self.scripts[script.__script_id__] = script.__script_obj__
            self.script_titles[script.__script_id__] = script.__script_title__
            self.script_settings[script.__script_id__] = script.__script_settings__ if hasattr(script, '__script_settings__') else {}
            self.script_commands[script.__script_id__] = script.__script_commands__ if hasattr(script, '__script_commands__') else lambda s: ""

        self._start_work_threads()

    def _start_work_threads(self):
        if hasattr(self, '_logger'):
            self._logger.debug('Starting work threads...')
        self._read_thread = Thread(target=self._reads_entrypoint,
                                   name='octoprint_automation_scripts_reads')
        self._read_thread.daemon = True
        self._read_thread.start()
        self._write_thread = Thread(target=self._writes_entrypoint,
                                    name='octoprint_automation_scripts_writes')
        self._write_thread.daemon = True
        self._write_thread.start()

    def _stop_work_threads(self):
        if hasattr(self, '_logger'):
            self._logger.debug('Stopping work threads...')
        self.future_serial.exit_work_threads(wait=True)

    def _reads_entrypoint(self):
        try:
            self.future_serial.work_off_reads()
        except Exception as e:
            self._logger.exception('Error while running read work thread: ' + str(e))

    def _writes_entrypoint(self):
        try:
            self.future_serial.work_off_writes()
        except Exception as e:
            self._logger.exception('Error while running write work thread: ' + str(e))

    ## MecodePlugin Interface  ##########################################

    def start(self, scriptname):
        if self.running:
            self._logger.warn("Can't start mecode script while previous one is running")
            return

        # This is an assertion about our expected internal state.
        if self.g is not None:
            raise RuntimeError("I was trying to start the script and expected self.g to be None, but it isn't")

        with self.read_lock:
            with self.write_lock:
                self.event.clear()
                self.running = True
                self.g = g = G(
                    print_lines=False,
                    aerotech_include=False,
                    direct_write=True,
                    direct_write_mode='serial',
                    layer_height = 0.19,
                    extrusion_width = 0.4,
                    filament_diameter = 1.75,
                    extrusion_multiplier = 1.00,
                    setup=False,
                )
                # We need a Printer instance for readline to work.
                g._p = Printer()
                self._mecode_thread = Thread(target=self.mecode_entrypoint,
                                             args=(scriptname,),
                                             name='mecode')
                self._mecode_thread.start()

    def mecode_entrypoint(self, scriptname):
        """
        Entrypoint for the mecode thread.  All exceptions are caught and logged.
        """
        try:
            self.execute_script(scriptname)
        except Exception as e:
            self._logger.exception('Error while running mecode: ' + str(e))

    def execute_script(self, scriptname):
        self._logger.info('Mecode script started')
        self.g._p.connect(self.s)
        self.g._p.start()

        try:
            settings = self._settings.get([scriptname])
            # Settings only contains changes, so merge with the defaults.
            full_settings = self.script_settings[scriptname].copy()
            full_settings.update(settings)
            self.so = scriptobj = self.scripts[scriptname](self.g, self._logger, full_settings)
            success, values = scriptobj.run()
            self.so = None
            if success:
                for key, val in values.iteritems():
                    self._settings.set([scriptname, key], str(val))
            else:
                self._logger.exception('Script failed, not saving values.')

        except Exception as e:
            self._logger.exception('Script was forcibly exited: ' + str(e))
            self.relinquish_control(wait=False)
            return

        self.relinquish_control()

    def relinquish_control(self, wait=True):
        if self.g is None:
            return
        self._logger.info('Resetting Line Number to 0')
        self.g._p.reset_linenumber()
        with self.read_lock:
            self._logger.info('Tearing down, waiting for buffer to clear: ' + str(wait))
            self.g.teardown(wait=wait)
            self.g = None
            self.running = False
            self._fake_ok = True
            self._temp_resp_len = 0
            self.event.set()
            self._logger.info('teardown finished, returning control to host')

    def single_threaded_readline(self, *args, **kwargs):
        if self.running:
            self.event.wait(2)
        with self.read_lock:
            if self._fake_ok:
                # Just finished running, and need to send fake ok.
                self._fake_ok = False
                resp = 'ok\n'
            elif not self.running:
                resp = self.s.readline(*args, **kwargs)
            else:
                # We are running.
                if len(self.g._p.temp_readings) > self._temp_resp_len:
                    # We have a new temp reading.  Respond with that.
                    self._temp_resp_len = len(self.g._p.temp_readings)
                    resp = self.g._p.temp_readings[-1]
                else:
                    if self.so is None or not self.so.response_string:
                        resp = 'Alignment script is running'
                    else:
                        resp = self.so.response_string
            return resp

    def single_threaded_write(self, data):
        with self.write_lock:
            if not self.running:
                return self.s.write(data)
            else:
                self._logger.warn('Write called when Mecode has control, ignoring: ' + str(data))

    def single_threaded_close(self):
        with self.write_lock:
            if self.g is not None:
                self.g.teardown(wait=False)
                self.g = None
            self.running = False
            self._fake_ok = False
            self._temp_resp_len = 0
            self.event.set()
        return self.s.close()

    ## serial.Serial Interface  ################################################

    def readline(self, *args, **kwargs):
        future = self.future_serial.future_readline(*args, **kwargs)
        # Block until a result is set.  If there was an exception, raise it.
        result = future.result()
        return result

    def write(self, data):
        future = self.future_serial.future_write(data)
        # Block until a result is set.  If there was an exception, raise it.
        result = future.result()
        return result

    def close(self):
        future = self.future_serial.future_close()
        # Block until a result is set.  If there was an exception, raise it.
        result = future.result()
        return result

    ## Plugin Hooks  ###########################################################

    #def print_started_sentinel(self, comm, phase, cmd, cmd_type, gcode, *args, **kwargs):
    #    if 'M900' in cmd:
    #        self.start()
    #        return None
    #    return cmd

    def serial_factory(self, comm_instance, port, baudrate, connection_timeout):
        if port == 'VIRTUAL':
            return None
        # The following is based on:
        # https://github.com/foosel/OctoPrint/blob/1.2.4/src/octoprint/util/comm.py#L1242
        if port is None or port == 'AUTO':
            # no known port, try auto detection
            comm_instance._changeState(comm_instance.STATE_DETECT_SERIAL)
            serial_obj = comm_instance._detectPort(True)
            if serial_obj is None:
                comm_instance._errorValue = 'Failed to autodetect serial port, please set it manually.'
                comm_instance._changeState(comm_instance.STATE_ERROR)
                comm_instance._log("Failed to autodetect serial port, please set it manually.")
                return None

            port = serial_obj.port

        # connect to regular serial port
        comm_instance._log("Connecting to: %s" % port)
        if baudrate == 0:
            # We can't call OctoPrint's private baudrateList() function, so
            # we've implemented our own.
            baudrates = self.baudrateList()
            # We changed the default to 250000 since that's what we usually use.
            serial_obj = serial.Serial(str(port), 250000 if 250000 in baudrates else baudrates[0], timeout=connection_timeout, writeTimeout=10000, parity=serial.PARITY_ODD)
        else:
            serial_obj = serial.Serial(str(port), baudrate, timeout=connection_timeout, writeTimeout=10000, parity=serial.PARITY_ODD)
        serial_obj.close()
        serial_obj.parity = serial.PARITY_NONE
        serial_obj.open()

        self.s = serial_obj
        self.future_serial.serial = self
        return self

    def get_update_information(self, *args, **kwargs):
        return dict(
            automation_scripts_plugin=dict(
                type="github_commit",
                user="Voxel8",
                repo="Octoprint-Automation-Scripts",
                branch="master",
                pip="https://github.com/Voxel8/OctoPrint-Automation-Scripts/archive/{target_version}.zip",
            )
        )

    def baudrateList(self):
        """
        Returns a list of baudrates to use when auto detecting.
        """
        # TODO: OctoPrint reads from the config to adjust this list.  Should we
        # do that also?
        ret = [250000, 230400, 115200, 57600, 38400, 19200, 9600]
        # Ensure this is always sorted.
        ret.sort(reverse=True)
        return ret

    ### EventHandlerPlugin API  ################################################

    def on_event(self, event, payload, *args, **kwargs):
        if event == 'Disconnecting' and self.running:
            # OctoPrint is trying to disconnect.  Interrupt automation.
            # Otherwise, we won't get the close call until after automation
            # finishes.
            self.relinquish_control(wait=False)
        if event == 'PrintCancelled':
            self.relinquish_control()
        if event == 'PrintPaused':
            if self.g is not None:
                self.g._p.paused = True
        if event == 'PrintResumed':
            if self.g is not None:
                self.g._p.paused = False
        if event == 'Connected':
            self.send_script_commands()

    def send_script_commands(self):
        for name, script_commands in self.script_commands.iteritems():
            cmd = script_commands(self._settings.get([name]))
            self._printer.commands(cmd)

    ### SettingsPlugin API  ####################################################

    def get_settings_defaults(self):
        # Settings are typically returned as OrderedDicts from the loaded
        # scripts so the script author can specify the order they appear in the
        # settings menu. Unfortunately pyyaml does not know how to serialize
        # OrderedDicts to we convert them to normal dicts here.
        return {id: dict(s) for id, s in self.script_settings.iteritems()}

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.send_script_commands()

    ### TemplatePlugin API  ####################################################

    def get_template_configs(self):
        return [
            dict(type="settings", template="automation_scripts_settings.jinja2", custom_bindings=False),
        ]

    def get_template_vars(self):
        settings = {}
        for script_id in self.scripts:
            settings[script_id] = self.script_settings[script_id].keys()
        return dict(settings=settings, titles=self.script_titles)

    ### AssetPlugin API  #######################################################

    def get_assets(self):
         return {
             "js": ["js/automation_scripts.js"],
         }

    ### SimpleApiPlugin API ####################################################

    def get_api_commands(self):
        return dict(
            [(script_id, []) for script_id in self.scripts]
        )

    def on_api_command(self, command, data):
        if command in self.scripts:
            self.start(command)

    def on_api_get(self, request):
        return flask.jsonify(script_titles=self.script_titles)

    ### ShutdownPlugin API ####################################################

    def on_shutdown(self):
        """
        Called upon the imminent shutdown of OctoPrint.
        """
        self._stop_work_threads()


def __plugin_load__():
    global __plugin_hooks__
    global __plugin_implementation__

    plugin = MecodePlugin()

    __plugin_implementation__ = plugin
    __plugin_hooks__ = {
        "octoprint.comm.transport.serial.factory": plugin.serial_factory,
        #"octoprint.comm.protocol.gcode.queuing": plugin.print_started_sentinel,
        "octoprint.plugin.softwareupdate.check_config": plugin.get_update_information,
    }
