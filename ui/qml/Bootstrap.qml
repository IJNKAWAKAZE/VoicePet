import QtQuick
import "components"

Item {
    objectName: "qmlBootstrap"
    width: 640
    height: 480
    visible: false

    QtObject {
        id: demoTheme
        property color surface: "white"
        property color surfaceAlt: "whitesmoke"
        property color text: "black"
        property color textMuted: "gray"
        property color disabledText: "gray"
        property color inverseText: "white"
        property color primary: "steelblue"
        property color interactionPrimary: "steelblue"
        property color primaryHover: "royalblue"
        property color primaryPressed: "navy"
        property color focus: "darkblue"
        property color border: "lightgray"
        property color danger: "firebrick"
        property color dangerSurface: "mistyrose"
        property color success: "seagreen"
        property color warning: "darkorange"
        property color info: "steelblue"
        property bool reduceMotion: true
        property int motionFast: 0
        property int motionStandard: 0
    }

    Column {
        AppButton { objectName: "demoButton"; theme: demoTheme; text: "按钮" }
        IconButton {
            objectName: "demoIconButton"
            theme: demoTheme
            accessibleName: "图标按钮"
        }
        AppTextField { objectName: "demoTextField"; theme: demoTheme }
        AppTextArea { objectName: "demoTextArea"; theme: demoTheme; width: 120; height: 60 }
        AppCard { objectName: "demoCard"; theme: demoTheme; width: 120; height: 60 }
        AppToggle { objectName: "demoToggle"; theme: demoTheme; text: "开关" }
        AppComboBox { objectName: "demoComboBox"; theme: demoTheme; model: ["选项"] }
        AppSlider { objectName: "demoSlider"; theme: demoTheme; width: 120 }
        StatusBadge { objectName: "demoBadge"; theme: demoTheme; text: "正常" }
    }
}
