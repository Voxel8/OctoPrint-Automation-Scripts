# coding=utf-8
import serial
import os
from threading import Thread, Event, Lock
import imp

from mecode import G
from mecode.printer import Printer
import octoprint.plugin
from octoprint.events import eventManager, Events
import flask


__author__ = "Jack Minardi <jack@voxel8.co>"
__copyright__ = "Copyright (C) 2015 Voxel8, Inc."


__plugin_name__ = "Automation Scripts"
__plugin_version__ = "0.3.0"
__plugin_author__ = "Jack Minardi"
__plugin_description__ = "Easily run a mecode script from OctoPrint"


SCRIPT_DIR = os.path.expanduser('~/.mecodescripts')

# Add our own custom events to the Event object
Events.AUTOMATION_SCRIPT_STARTED = "AutomationScriptStarted"
Events.AUTOMATION_SCRIPT_FINISHED = "AutomationScriptFinished"
Events.AUTOMATION_SCRIPT_ERROR = "AutomationScriptError"
Events.AUTOMATION_SCRIPT_FAILED = "AutomationScriptFailed"
Events.AUTOMATION_SCRIPT_STATUS_CHANGED = "AutomationScriptStatusChanged"


class MecodePlugin(octoprint.plugin.EventHandlerPlugin,
                   octoprint.plugin.SettingsPlugin,
                   octoprint.plugin.TemplatePlugin,
                   octoprint.plugin.AssetPlugin,
                   octoprint.plugin.SimpleApiPlugin,
                   ):

    def __init__(self):
        if not os.path.exists(SCRIPT_DIR):
            self._logger.warn('Script directory does not exist')
            return

        self.s = None
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
        self.active_script_id = None
        self._old_script_status = None
        self.saved_line_number = None
        self._disconnect_lock = Lock()
        self._cancelled = False

        self.scripts = {}
        self.script_titles = {}
        self.script_settings = {}
        self.script_commands = {}
        scriptdir = SCRIPT_DIR
        for i, filename in enumerate(
            [f for f in os.listdir(scriptdir) if f.endswith('.py')]
        ):
            path = os.path.join(scriptdir, filename)
            script = imp.load_source('mecodescript' + str(i), path)
            # script ids can not contain dashes or spaces
            id = script.__script_id__.replace('-', '_').replace(' ', '_')
            self.scripts[id] = script.__script_obj__
            self.script_titles[id] = script.__script_title__
            self.script_settings[id] = script.__script_settings__ if hasattr(
                script, '__script_settings__') else {}
            self.script_commands[id] = script.__script_commands__ if hasattr(
                script, '__script_commands__') else lambda s: ""

    # MecodePlugin Interface  ##########################################

    def _is_running(self):
        return self.running

    def start(self, script_id, extra_args={}):
        self._cancelled = False
        if self.running:
            self._logger.warn(
                "Can't start mecode script while previous one is running")
            return

        # This is an assertion about our expected internal state.
        if self.g is not None:
            raise RuntimeError(
                "I was trying to start the script and expected self.g to"
                "be None, but it isn't")

        payload = {'id': script_id,
                   'title': self.script_titles[script_id]}
        eventManager().fire(Events.AUTOMATION_SCRIPT_STARTED, payload)
        with self.read_lock:
            with self.write_lock:
                self.event.clear()
                self.running = True
                self.g = g = G(
                    print_lines=False,
                    aerotech_include=False,
                    direct_write=True,
                    direct_write_mode='serial',
                    layer_height=0.19,
                    extrusion_width=0.4,
                    filament_diameter=1.75,
                    extrusion_multiplier=1.00,
                    setup=False,
                )
                # We need a Printer instance for readline to work.
                g._p = Printer()
                self._mecode_thread = Thread(target=self.mecode_entrypoint,
                                             args=(script_id, extra_args),
                                             name='mecode')
                self._mecode_thread.start()
                self.active_script_id = script_id

    def mecode_entrypoint(self, script_id, extra_args):
        """
        Entrypoint for the mecode thread. All exceptions are caught and logged.
        """
        try:
            self.execute_script(script_id, extra_args)
        except Exception as e:
            self._logger.exception('Error while running mecode: ' + str(e))
            self.running = False
            self.active_script_id = None
            self._old_script_status = None
            self.g = None
            if not self._cancelled:
                payload = {'id': script_id,
                           'title': self.script_titles[script_id],
                           'error': str(e)}
                eventManager().fire(Events.AUTOMATION_SCRIPT_ERROR, payload)

    def execute_script(self, script_id, extra_args):
        self._logger.info('Mecode script started')
        self.saved_line_number = self._printer._comm._currentLine - 1
        self.g._p.connect(self.s)
        self.g._p.start()
        self.g._p.reset_linenumber()  # ensure we start off in a clean state

        try:
            settings = self._settings.get([script_id])
            # Settings only contains changes, so merge with the defaults.
            full_settings = self.script_settings[script_id].copy()
            full_settings.update(settings)
            self.so = scriptobj = self.scripts[script_id](
                self.g, self._logger, full_settings)

            # Actually run the user script.
            try:
                raw_result = scriptobj.run(**extra_args)
            except TypeError as e:  # accepting extra_args is optional
                self._logger.info(
                    "Retrying script with no arguments, error was: " + str(e))
                raw_result = scriptobj.run()
            # Merge raw result with defaults.
            result = {
                'wait_for_buffer': True,
                'storage': {},
                'success': True,
                'failure_reason': '',
                'success_message': '',
            }
            # Handle legacy interface.
            if isinstance(raw_result, tuple):
                success, values = raw_result
                raw_result = {'storage': values} if success else None
            if raw_result is not None:
                result.update(raw_result)

            # Ensure that any commands sent to the printer are actually
            # executed *before* cleaning up the script object.
            if result['wait_for_buffer']:
                self.g.write("M400", resp_needed=True)
            self.so = None

            # Store script's settings.
            if result['success'] and result['storage'] is not None:
                for key, val in result['storage'].iteritems():
                    self._settings.set([script_id, key], str(val))

        except Exception as e:
            self._logger.exception('Script was forcibly exited: ' + str(e))
            if not self._cancelled:
                payload = {'id': script_id,
                           'title': self.script_titles[script_id],
                           'error': str(e)}
                eventManager().fire(Events.AUTOMATION_SCRIPT_ERROR, payload)
            self.relinquish_control(wait=False)
            return

        self.relinquish_control()

        if result['success']:
            payload = {'id': script_id,
                       'title': self.script_titles[script_id],
                       'result': result['storage'],
                       'success_message': result['success_message']}
            eventManager().fire(Events.AUTOMATION_SCRIPT_FINISHED, payload)
        else:
            payload = {'id': script_id,
                       'title': self.script_titles[script_id],
                       'failure_reason': result['failure_reason']}
            eventManager().fire(Events.AUTOMATION_SCRIPT_FAILED, payload)

    def relinquish_control(self, wait=True):
        with self._disconnect_lock:
            if self.g is None:
                return
            self._logger.info('Resetting Line Number to %s' %
                              self.saved_line_number)
            self.g._p.reset_linenumber(self.saved_line_number)
            with self.read_lock:
                self._logger.info(
                    'Tearing down, waiting for buffer to clear: ' + str(wait))
                self.g.teardown(wait=wait)
                self.g = None
                self.running = False
                self.active_script_id = None
                self._old_script_status = None
                self._fake_ok = True
                self._temp_resp_len = 0
                self.saved_line_number = None
                self.event.set()
                self._logger.info(
                    'teardown finished, returning control to host')

    # serial.Serial Interface  ##############################################

    def readline(self, *args, **kwargs):
        if self.running:
            self.event.wait(2)
        with self.read_lock:
            if self._fake_ok:
                # Just finished running, and need to send fake ok.
                self._fake_ok = False
                resp = 'ok\n'
            elif not self.running:
                resp = self.s.readline(*args, **kwargs)
            else:  # We are running.
                resp = self._generate_fake_response()
            return resp

    def write(self, data):
        with self.write_lock:
            if not self.running:
                return self.s.write(data)
            else:
                self._logger.warn(
                    'Write called when Mecode has control, ignoring: ' +
                    str(data))

    def close(self):
        with self.write_lock:
            if self.g is not None:
                self.g.teardown(wait=False)
                self.g = None
            self.running = False
            self._fake_ok = False
            self._temp_resp_len = 0
            self.event.set()
        return self.s.close()

    def _generate_fake_response(self):
        if len(self.g._p.temp_readings) > self._temp_resp_len:
            # We have a new temp reading.  Respond with that.
            self._temp_resp_len = len(self.g._p.temp_readings)
            resp = self.g._p.temp_readings[-1]
        else:
            resp = '>>> Automation Script Running'
            if hasattr(self.so, 'script_status') and self.so.script_status:
                resp += ': ' + self.so.script_status
                if self.so.script_status != self._old_script_status:
                    self._old_script_status = self.so.script_status
                    payload = {'id': self.active_script_id,
                               'title':
                               self.script_titles[self.active_script_id],
                               'status': self.so.script_status}
                    eventManager().fire(
                        Events.AUTOMATION_SCRIPT_STATUS_CHANGED, payload)
        return resp

    # Plugin Hooks  #########################################################

    def serial_factory(self, comm_instance, port, baudrate,
                       connection_timeout):
        if port == 'VIRTUAL':
            return None
        # The following is based on:
        # https://github.com/foosel/OctoPrint/blob/1.2.4/src/octoprint/util/comm.py#L1242
        if port is None or port == 'AUTO':
            # no known port, try auto detection
            comm_instance._changeState(comm_instance.STATE_DETECT_SERIAL)
            serial_obj = comm_instance._detectPort(True)
            if serial_obj is None:
                comm_instance._errorValue = ("Failed to autodetect serial "
                                             "port, please set it manually.")
                comm_instance._changeState(comm_instance.STATE_ERROR)
                comm_instance._log("Failed to autodetect serial port, "
                                   "please set it manually.")
                return None

            port = serial_obj.port

        # connect to regular serial port
        comm_instance._log("Connecting to: %s" % port)
        if baudrate == 0:
            # We can't call OctoPrint's private baudrateList() function, so
            # we've implemented our own.
            baudrates = self.baudrateList()
            # We changed the default to 250000 since that's what we usually
            # use.
            serial_obj = serial.Serial(
                str(port), 250000 if 250000 in baudrates else baudrates[0],
                timeout=connection_timeout, writeTimeout=10000,
                parity=serial.PARITY_ODD)
        else:
            serial_obj = serial.Serial(
                str(port), baudrate, timeout=connection_timeout,
                writeTimeout=10000, parity=serial.PARITY_ODD)
        serial_obj.close()
        serial_obj.parity = serial.PARITY_NONE
        serial_obj.open()

        self.s = serial_obj
        return self

    def get_update_information(self, *args, **kwargs):
        return dict(
            automation_scripts_plugin=dict(
                type="github_commit",
                user="Voxel8",
                repo="Octoprint-Automation-Scripts",
                branch="master",
                pip=("https://github.com/Voxel8/OctoPrint-Automation-Scripts/"
                     "archive/{target_version}.zip"),
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

    # EventHandlerPlugin API  ##############################################

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
        for script_id, script_commands in self.script_commands.iteritems():
            full_settings = self.script_settings[script_id].copy()
            full_settings.update(self._settings.get([script_id]))
            cmd = script_commands(full_settings)
            self._printer.commands(cmd)

    # SettingsPlugin API  ##################################################

    def get_settings_defaults(self):
        # Settings are typically returned as OrderedDicts from the loaded
        # scripts so the script author can specify the order they appear in the
        # settings menu. Unfortunately pyyaml does not know how to serialize
        # OrderedDicts to we convert them to normal dicts here.
        return {id: dict(s) for id, s in self.script_settings.iteritems()}

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self.send_script_commands()

    # TemplatePlugin API  ##################################################

    def get_template_configs(self):
        return [
            dict(type="settings",
                 template="automation_scripts_settings.jinja2",
                 custom_bindings=False),
        ]

    def get_template_vars(self):
        settings = {}
        for script_id in self.scripts:
            settings[script_id] = self.script_settings[script_id].keys()
        return dict(settings=settings, titles=self.script_titles)

    # AssetPlugin API  #####################################################

    def get_assets(self):
        return {
            "js": ["js/automation_scripts.js"],
        }

    # SimpleApiPlugin API ##################################################

    def get_api_commands(self):
        return dict(
            [(script_id, []) for script_id in self.scripts] +
            [('cancel', [])]
        )

    def on_api_command(self, command, data):
        if command == 'cancel':
            self._cancelled = True
            self.relinquish_control(wait=False)
        elif command in self.scripts:
            del data['command']
            self.start(command, data)

    def on_api_get(self, request):
        if self.active_script_id is not None:
            title = self.script_titles[self.active_script_id]
        else:
            title = None
        return flask.jsonify(running=self._is_running(),
                             current_script_title=title,
                             script_titles=self.script_titles)


def __plugin_load__():
    global __plugin_hooks__
    global __plugin_implementation__

    plugin = MecodePlugin()

    __plugin_implementation__ = plugin
    __plugin_hooks__ = {
        "octoprint.comm.transport.serial.factory": plugin.serial_factory,
    }
