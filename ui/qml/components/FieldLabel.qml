import QtQuick

Text {
    required property QtObject theme
    color: theme.text
    font.family: "Microsoft YaHei UI"
    font.pixelSize: 13
    font.weight: Font.DemiBold
    wrapMode: Text.Wrap
}
