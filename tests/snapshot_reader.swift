// Appended to the production connection in one compilation unit for private access.
extension CachedSnapshotConnection {
    func fixtureRead(_ handle: FileHandle) throws -> Data { try readLine(handle) }
    func fixtureBuffer(_ data: Data) { buffered = data }
    func fixtureReset() { reset() }
    func fixtureRetryNow() { nextAttempt = 0 }
    var fixtureIsReset: Bool { buffered.isEmpty && process == nil && input == nil && output == nil }
}

func check(_ value: @autoclosure () -> Bool, _ message: String) {
    precondition(value(), message)
}
func rejects(_ body: () throws -> Void) {
    do { try body(); preconditionFailure("expected read failure") } catch { }
}
func stream(_ chunks: [Data], _ body: (CachedSnapshotConnection, FileHandle) throws -> Void) throws {
    let pipe = Pipe(), connection = CachedSnapshotConnection(), finished = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        defer { try? pipe.fileHandleForWriting.close(); finished.signal() }
        for chunk in chunks {
            try? pipe.fileHandleForWriting.write(contentsOf: chunk)
            // Encourage fragmentation without relying on a particular kernel pipe size.
            usleep(1000)
        }
    }
    defer { try? pipe.fileHandleForReading.close(); finished.wait() }
    try body(connection, pipe.fileHandleForReading)
}
signal(SIGPIPE, SIG_IGN)
try stream([Data("abc".utf8), Data("def\n".utf8)]) { c, h in
    let data = try c.fixtureRead(h); check(data == Data("abcdef".utf8), "fragmentation")
}
try stream([Data("one\ntwo\n\nthree\n".utf8)]) { c, h in
    for value in ["one", "two", "", "three"] {
        let data = try c.fixtureRead(h); check(data == Data(value.utf8), "coalesced frames")
    }
}
try stream([Data("first\npar".utf8), Data("tial\n".utf8)]) { c, h in
    let first = try c.fixtureRead(h), second = try c.fixtureRead(h)
    check(first == Data("first".utf8) && second == Data("partial".utf8), "leftover plus new bytes")
}
let sliced = CachedSnapshotConnection(), slicedPipe = Pipe()
sliced.fixtureBuffer(Data("prefixleft\nright\n".utf8).dropFirst(6))
for value in ["left", "right"] {
    let data = try sliced.fixtureRead(slicedPipe.fileHandleForReading)
    check(data == Data(value.utf8), "nonzero Data startIndex")
}
let unicode = Data("核心温度🌡️".utf8)
try stream(unicode.map { Data([$0]) } + [Data([10])]) { c, h in
    let data = try c.fixtureRead(h); check(data == unicode, "split UTF-8")
}
let large = Data(repeating: 120, count: 2 * 1024 * 1024)
try stream([large, Data([10])]) { c, h in
    let data = try c.fixtureRead(h); check(data == large, "large frame")
}
try stream([Data(repeating: 97, count: 8192), Data([10])]) { c, h in
    let data = try c.fixtureRead(h); check(data.count == 8192, "chunk boundary")
}
try stream([Data("partial".utf8)]) { c, h in rejects { _ = try c.fixtureRead(h) } }
try stream([]) { c, h in rejects { _ = try c.fixtureRead(h) } }
let idle = Pipe(), timed = CachedSnapshotConnection(), start = ProcessInfo.processInfo.systemUptime
rejects { _ = try timed.fixtureRead(idle.fileHandleForReading) }
let elapsed = ProcessInfo.processInfo.systemUptime - start
check(elapsed >= 3.8 && elapsed < 6, "4 second timeout")
try idle.fileHandleForWriting.close(); try idle.fileHandleForReading.close()
let boundary = CachedSnapshotConnection(), unused = Pipe()
boundary.fixtureBuffer(Data(repeating: 97, count: 64 * 1024 * 1024 - 1) + Data([10]))
let maximum = try boundary.fixtureRead(unused.fileHandleForReading)
check(maximum.count == 64 * 1024 * 1024 - 1, "exact limit")
boundary.fixtureBuffer(Data(repeating: 97, count: 64 * 1024 * 1024 + 1))
rejects { _ = try boundary.fixtureRead(unused.fileHandleForReading) }
boundary.fixtureReset(); check(boundary.fixtureIsReset, "reset discards partial frame")
let reconnect = CachedSnapshotConnection()
rejects { _ = try reconnect.read(visible: true, priorityProject: nil) }
check(reconnect.fixtureIsReset, "EOF resets process and buffers")
let early = ProcessInfo.processInfo.systemUptime
rejects { _ = try reconnect.read(visible: true, priorityProject: nil) }
check(ProcessInfo.processInfo.systemUptime - early < 0.5, "backoff rejects promptly")
reconnect.fixtureRetryNow()
rejects { _ = try reconnect.read(visible: true, priorityProject: nil) }
check(reconnect.fixtureIsReset, "invalid JSON resets connection")
reconnect.fixtureRetryNow()
for _ in 0..<2 {
    let response = try reconnect.read(visible: true, priorityProject: nil)
    check(response["value"] as? String == "恢复成功", "reconnect and persistent worker")
}
reconnect.fixtureReset()
print("PASS 12: fragments, coalescing, UTF-8, large, boundary, partial EOF, EOF, timeout, limit, oversize/reset, backoff, reconnect/JSON")
