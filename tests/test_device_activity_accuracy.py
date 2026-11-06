from zai_devdashd.probes import device_runtime_command


def test_compile_and_observer_commands_do_not_claim_device_runtime():
    assert not device_runtime_command("/Applications/Xcode.app/usr/bin/xcodebuild build-for-testing -destination id=Tablet101")
    assert not device_runtime_command("/usr/bin/xcrun devicectl list devices")
    assert not device_runtime_command("/usr/bin/xcrun devicectl device install app --device Tablet101 app")
    assert not device_runtime_command("/bin/echo xcodebuild test -destination id=Tablet101")


def test_only_actual_test_or_launch_executables_claim_runtime():
    assert device_runtime_command("/Applications/Xcode.app/usr/bin/xcodebuild test-without-building -destination id=Tablet101")
    assert device_runtime_command("/usr/bin/xcrun devicectl device process launch --device Tablet101 app")


def test_action_option_values_and_wrapped_tool_options():
    assert not device_runtime_command('/usr/bin/xcodebuild -scheme test build-for-testing -destination id=Tablet101')
    assert device_runtime_command('/usr/bin/xcrun --sdk iphoneos xcodebuild test-without-building -destination id=Tablet101')
    assert device_runtime_command('/usr/bin/devicectl --timeout 30 device process launch --device Tablet101 app')


def test_runtime_binding_uses_destination_not_derived_data_name():
    from zai_devdashd.probes import device_runtime_targets, device_aliases, referenced
    command = '/usr/bin/xcodebuild test-without-building -derivedDataPath /work/Phone102-tests -destination id=Tablet101'
    targets = device_runtime_targets(command)
    assert targets == ['Tablet101']
    assert not referenced(device_aliases('Phone102', '', ''), targets)
