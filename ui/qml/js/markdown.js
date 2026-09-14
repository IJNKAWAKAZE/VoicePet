// Qt 的 Markdown 导入器把代码块段落标成不换行，整段交给一个控件渲染会画出容器之外
function splitBlocks(source) {
    var lines = String(source || "").split("\n")
    var blocks = []
    var buffer = []
    var fence = ""

    function flush(kind) {
        if (buffer.length === 0)
            return
        var text = buffer.join("\n")
        buffer = []
        if (kind === "text" && text.trim().length === 0)
            return
        blocks.push({ "kind": kind, "text": text })
    }

    for (var index = 0; index < lines.length; ++index) {
        var match = /^[ \t]{0,3}(`{3,}|~{3,})/.exec(lines[index])
        if (match === null) {
            buffer.push(lines[index])
        } else if (fence.length === 0) {
            flush("text")
            fence = match[1].charAt(0)
        } else if (match[1].charAt(0) === fence) {
            flush("code")
            fence = ""
        } else {
            buffer.push(lines[index])
        }
    }
    flush(fence.length === 0 ? "text" : "code")
    if (blocks.length === 0)
        blocks.push({ "kind": "text", "text": "" })
    return blocks
}
