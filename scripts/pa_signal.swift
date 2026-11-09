// Send a signal to the running Rozan app: swift scripts/pa_signal.swift toggle|fullapp
import Foundation
let name = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "toggle"
DistributedNotificationCenter.default().postNotificationName(.init("com.personal-agent.\(name)"), object: nil, userInfo: nil, deliverImmediately: true)
