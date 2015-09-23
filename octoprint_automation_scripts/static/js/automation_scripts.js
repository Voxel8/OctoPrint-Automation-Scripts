$(function() {
    function AutomationScriptsViewModel(parameters) {
        var self = this;

        self.onStartup = function() {
            $.get('/api/plugin/automation_scripts', success=function(data){
                self.scriptTitles = data.script_titles;
            });
        };

        self.getAdditionalControls = function() {
            var buttons = [];
            for (var scriptName in self.scriptTitles) {
                var scriptTitle = self.scriptTitles[scriptName];
                buttons.push(
                    {
                        'javascript': '$.ajax({url: "/api/plugin/automation_scripts", type: "POST", contentType: "application/json; charset=utf-8", dataType: "json", data: JSON.stringify({ "command": "'+scriptName+'"})});',
                        'name': scriptTitle
                    }
                );
            }
            return [{
                'children': buttons,
                'layout': 'vertical',
                'name': 'Automation Scripts'
            }];
        };
    }

    // This is how our plugin registers itself with the application, by adding some configuration information to
    // the global variable ADDITIONAL_VIEWMODELS
    ADDITIONAL_VIEWMODELS.push([
        // This is the constructor to call for instantiating the plugin
        AutomationScriptsViewModel,

        // This is a list of dependencies to inject into the plugin, the order which you request here is the order
        // in which the dependencies will be injected into your view model upon instantiation via the parameters
        // argument
        [],//"loginStateViewModel", "settingsViewModel", "temperatureViewModel"],

        // Finally, this is the list of all elements we want this view model to be bound to.
        []
    ]);
});
