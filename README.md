# OctoPrint-Automation-Scripts

This plugin looks for python files inside of '~/.mecodescripts' and then loads them in
to be run from within OctoPrint. The scripts need to define a few magic variables:
    
### `__script_obj__`
  
  A python object that contains the script logic. This object's `__init__` method must take
  three arguments:
  
  g : An instance of a `G` object from the [mecode](github.com/jminardi/mecode) library. 
  This object is already connected to the printer.
  
  logger : An instance of `logging.Logger` that should be used to log data for the user.
  
  settings : A dict containing the stored user settings for your script.
  
  Your object must also define a `run()` method that takes no arguments. This method is called
  when the user clicks the button corresponging to your script. The `run()` method is expected
  to return two things: A boolean value representing whether or not the script completed
  successfully, and a dictionary of settings items and their values to persist in the settings
  store.
  
  Finally, your object should have an attribute `script_status`. If set to anything other than
  `None` this value will be echoed to OctoPrint while the script is running.
  
### `__script_id__`
  
  The identifier for this script. Must be globally unique and can not contain spaces or dashes.
  The identifier also can not be the string `cancel`.
    
### `__script_title__`
  
  The human-readble title for the script. Will be used for the button text and settings header.
    
### `__script_settings__` (optional)
  
  A dictionary of values the user can feed into your script. The user can update these values in
  the OctoPrint settings dialog.
    
### `__script_commands__` (optional)
  
  A callable taking as an argument your settings dict. Should return a string or list of strings
  that will be sent to the printer on connect.

## Events

This plugin also hooks into the OctoPrint Eventing system. Four new events are defined:

### "AutomationScriptStarted"

  Fired when a script first starts.

  payload keys:

  * `id` : The id of the script that just started.
  * `title` : The title of the script that just started.

### "AutomationScriptFinished"

  Fired when a script finishes.

  payload keys:

  * `id` : The id of the script that finished.
  * `title` : The title of the script that finished.
  * `result` : The values returned from the finished script.

### "AutomationScriptError"

  Fired when a script encounter's an error.

  payload keys:

  * `id` : The id of the script that errored.
  * `title` : The title of the script that errored.
  * `error` : The error message from the script that errored.

### "AutomationScriptStatusChanged"

  Fired when a script's status message changes.

  payload keys:

  * `id` : The id of the script.
  * `title` : The title of the script.
  * `status` : The new status message.

