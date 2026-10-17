#!/usr/bin/env python3
"""otomo-mobile 検証用の BLE Provisioning Service スタブ
(docs/requirements.md 4.3.1, ADR-0007)。

BlueZ の D-Bus GATT API を使い、Provisioning Service を advertise・実装
する。実ハードウェア（実際の WiFi 再接続等）は行わず、ssid/psk の書き込み
受信後に接続成功を模擬する検証用スタブ（本物の desk-companion 実装では
ない）。

ベース: bluez/test/example-gatt-server, bluez/test/example-advertisement
(SPDX-License-Identifier: LGPL-2.1-or-later) を Provisioning Service 用に
書き換えたもの。

使い方:
    python3 otomo_ble_provisioning_server.py

依存パッケージ:
    sudo apt install -y python3-dbus python3-gi bluez
"""
from __future__ import annotations

import logging
import secrets
import socket

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("otomo_ble_provisioning_server")

BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"

LOCAL_NAME = "otomo-9c3e"
PROVISIONING_SERVICE_UUID = "28097c74-a5e8-4dcd-8b86-1be6576f6fe9"
WIFI_SSID_CHRC_UUID = "cbb6c752-8368-4a03-bbad-da54b75458fa"
WIFI_PSK_CHRC_UUID = "6f470ac7-2683-4fb6-ab83-7110b4713eb8"
PROVISION_STATUS_CHRC_UUID = "59969394-84a7-48a6-b50c-fa9573955ba1"
DEVICE_TOKEN_CHRC_UUID = "cfef518e-ca95-40cc-8430-4ab9e61de174"
ASSIGNED_IP_CHRC_UUID = "3b5abc00-7b30-4f95-85c9-1994429b6b0d"

STATUS_IDLE = 0
STATUS_CONNECTING = 1
STATUS_SUCCESS = 2
STATUS_FAILURE = 3


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


class Application(dbus.service.Object):
    """org.bluez.GattApplication1 interface implementation."""

    def __init__(self, bus):
        self.path = "/"
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.characteristics:
                response[chrc.get_path()] = chrc.get_properties()
        return response


class Service(dbus.service.Object):
    """org.bluez.GattService1 interface implementation."""

    PATH_BASE = "/org/bluez/otomo/service"

    def __init__(self, bus, index, uuid):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = uuid
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                "UUID": self.uuid,
                "Primary": True,
                "Characteristics": dbus.Array(
                    [c.get_path() for c in self.characteristics], signature="o"
                ),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_SERVICE_IFACE]


class Characteristic(dbus.service.Object):
    """org.bluez.GattCharacteristic1 interface implementation."""

    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + "/char" + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                "Service": self.service.get_path(),
                "UUID": self.uuid,
                "Flags": self.flags,
                "Descriptors": dbus.Array([], signature="o"),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class ProvisioningService(Service):
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, PROVISIONING_SERVICE_UUID)
        self.ssid = None
        self.psk = None
        self.device_token = bytes(16)
        self.assigned_ip = ""
        self._provisioning_started = False

        self.status_chrc = ProvisionStatusCharacteristic(bus, 2, self)
        self.add_characteristic(WifiSsidCharacteristic(bus, 0, self))
        self.add_characteristic(WifiPskCharacteristic(bus, 1, self))
        self.add_characteristic(self.status_chrc)
        self.add_characteristic(DeviceTokenCharacteristic(bus, 3, self))
        self.add_characteristic(AssignedIpCharacteristic(bus, 4, self))

    def on_ssid_written(self, ssid):
        self.ssid = ssid
        log.info("wifi_ssid written: %r", ssid)
        self._maybe_start_provisioning()

    def on_psk_written(self, psk):
        self.psk = psk
        log.info("wifi_psk written: (masked, %d bytes)", len(psk))
        self._maybe_start_provisioning()

    def _maybe_start_provisioning(self):
        if self._provisioning_started:
            return
        if self.ssid is None or self.psk is None:
            return
        self._provisioning_started = True
        self.status_chrc.update_status(STATUS_CONNECTING)
        GLib.timeout_add(300, self._finish_provisioning)

    def _finish_provisioning(self):
        self.device_token = secrets.token_bytes(16)
        self.assigned_ip = _local_ip()
        log.info("provisioning success, assigned_ip=%s", self.assigned_ip)
        self.status_chrc.update_status(STATUS_SUCCESS)
        return False


class WifiSsidCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, WIFI_SSID_CHRC_UUID, ["write"], service)

    def WriteValue(self, value, options):
        self.service.on_ssid_written(bytes(value).decode("utf-8"))


class WifiPskCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, WIFI_PSK_CHRC_UUID, ["write"], service)

    def WriteValue(self, value, options):
        self.service.on_psk_written(bytes(value).decode("utf-8"))


class ProvisionStatusCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(
            self, bus, index, PROVISION_STATUS_CHRC_UUID, ["notify"], service
        )
        self.notifying = False
        self.value = STATUS_IDLE

    def update_status(self, value):
        self.value = value
        if not self.notifying:
            return
        self.PropertiesChanged(
            GATT_CHRC_IFACE, {"Value": dbus.Array([dbus.Byte(value)], signature="y")}, []
        )

    def StartNotify(self):
        self.notifying = True

    def StopNotify(self):
        self.notifying = False


class DeviceTokenCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, DEVICE_TOKEN_CHRC_UUID, ["read"], service)

    def ReadValue(self, options):
        return [dbus.Byte(b) for b in self.service.device_token]


class AssignedIpCharacteristic(Characteristic):
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, ASSIGNED_IP_CHRC_UUID, ["read"], service)

    def ReadValue(self, options):
        return [dbus.Byte(b) for b in self.service.assigned_ip.encode("utf-8")]


class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/bluez/otomo/advertisement"

    def __init__(self, bus, index):
        self.path = self.PATH_BASE + str(index)
        self.ad_type = "peripheral"
        self.service_uuids = [PROVISIONING_SERVICE_UUID]
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        # 128bit Service UUID(18B)+Flags(3B)=21B。LocalNameまで載せると
        # legacy advertising の31B上限を超えるため、LocalNameは
        # GAP Device Name (GATT) 解決に任せ、広告には含めない。
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": self.ad_type,
                "ServiceUUIDs": dbus.Array(self.service_uuids, signature="s"),
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        log.info("advertisement released")


def find_adapter(bus, iface):
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    for path, props in remote_om.GetManagedObjects().items():
        if iface in props:
            return path
    return None


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    gatt_adapter = find_adapter(bus, GATT_MANAGER_IFACE)
    ad_adapter = find_adapter(bus, LE_ADVERTISING_MANAGER_IFACE)
    if not gatt_adapter or not ad_adapter:
        log.error("GattManager1/LEAdvertisingManager1 not found")
        return

    adapter_props = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, gatt_adapter), DBUS_PROP_IFACE
    )
    adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
    adapter_props.Set("org.bluez.Adapter1", "Alias", dbus.String(LOCAL_NAME))

    app = Application(bus)
    app.add_service(ProvisioningService(bus, 0))

    service_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, gatt_adapter), GATT_MANAGER_IFACE
    )
    ad_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, ad_adapter), LE_ADVERTISING_MANAGER_IFACE
    )
    advertisement = Advertisement(bus, 0)

    mainloop = GLib.MainLoop()

    def on_app_registered():
        log.info("GATT application registered (%s)", PROVISIONING_SERVICE_UUID)

    def on_app_register_error(error):
        log.error("failed to register GATT application: %s", error)
        mainloop.quit()

    def on_ad_registered():
        log.info("advertising %s as %s", LOCAL_NAME, PROVISIONING_SERVICE_UUID)

    def on_ad_register_error(error):
        log.error("failed to register advertisement: %s", error)
        mainloop.quit()

    service_manager.RegisterApplication(
        app.get_path(), {}, reply_handler=on_app_registered, error_handler=on_app_register_error
    )
    ad_manager.RegisterAdvertisement(
        advertisement.get_path(), {}, reply_handler=on_ad_registered, error_handler=on_ad_register_error
    )

    mainloop.run()


if __name__ == "__main__":
    main()
