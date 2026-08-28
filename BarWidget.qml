import QtQuick
import Quickshell.Io
import qs.Ui

BarWidget {
  id: root
  moduleName: "io.github.sahzudin.omarchy-google-search"

  readonly property string barIcon: setting("icon", "󰍋")
  readonly property string defaultEngine: setting("defaultEngine", "google")
  readonly property string openShortcut: setting("openShortcut", "SUPER + ALT + P")

  readonly property var engines: [
    { id: "google",       label: "Google",         prefix: "https://www.google.com/search?q=",          icon: 0xF1A0 },
    { id: "chatgpt",      label: "ChatGPT",        prefix: "https://chatgpt.com/?q=",                   icon: 0xEC81, verb: "Ask" },
    { id: "claude",       label: "Claude",         prefix: "https://claude.ai/new?q=",                  icon: 0xEC82, verb: "Ask" },
    { id: "bing",         label: "Bing",           prefix: "https://www.bing.com/search?q=",            icon: 0xF00A4 },
    { id: "duckduckgo",   label: "DuckDuckGo",     prefix: "https://duckduckgo.com/?q=",               icon: 0xF01E5 },
    { id: "github",       label: "GitHub",         prefix: "https://github.com/search?q=",              icon: 0xF09B },
    { id: "wikipedia",    label: "Wikipedia",      prefix: "https://en.wikipedia.org/w/index.php?search=", icon: 0xF266 },
    { id: "youtube",      label: "YouTube",        prefix: "https://www.youtube.com/results?search_query=", icon: 0xF167 },
    { id: "reddit",       label: "Reddit",         prefix: "https://www.reddit.com/search/?q=",         icon: 0xF1A1 },
    { id: "stackoverflow",label: "Stack Overflow", prefix: "https://stackoverflow.com/search?q=",        icon: 0xF16C, abbr: "Stack" }
  ]

  readonly property bool opened: panelLoader.item
    ? panelLoader.item.opened === true
    : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true
    : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
    panelLoader.item.engines = root.engines
    panelLoader.item.defaultEngine = root.defaultEngine
    panelLoader.item.openShortcut = root.openShortcut
    panelLoader.item.barIcon = root.barIcon
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: root.moduleName

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barIcon
    tooltipText: "Web search — click to enter a query"
    horizontalMargin: 8.75
    verticalPadding: 8.75

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
    }
  }
}
