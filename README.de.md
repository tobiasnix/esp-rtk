# ESP-RTK – Kurzanleitung

ESP-RTK ist eine MicroPython-Firmware für ein kompaktes RTK-Feldgerät aus
ESP32-S3 und Quectel-LC29H-Empfänger. Es empfängt NTRIP-Korrekturdaten über
WLAN, zeigt die Messqualität im lokalen Webportal und gibt NMEA über
Bluetooth LE oder TCP aus. Projekte, Linien und Objektpunkte werden auf dem
Gerät gespeichert und als GeoJSON, CSV oder Sicherung exportiert; aus GeoJSON
eingelesene Zielpunkte lassen sich im Feld abstecken.

Aktuelle Firmware: **V16.0.0**

Diese Seite ist die deutsche Kurzanleitung für Anwenderinnen und Anwender.
Installation, Hardware, Schnittstellen und alle technischen Angaben stehen in
der englischen Dokumentation, beginnend mit [README.md](README.md).

## In sechs Schritten starten

1. Gerät einschalten und den Start abwarten.
2. Den WLAN-QR-Code auf dem Geräteetikett scannen. Ist das Einrichtungs-WLAN
   nicht sichtbar, RST drücken und danach die **BOOT-Taste drei Sekunden**
   halten. Das Einrichtungsnetz heißt nach dem Gerät, zum Beispiel
   `RTK-A1B2C3-SETUP`, und das Portal antwortet unter `http://192.168.4.1/`.
3. Das Captive Portal öffnet die Feldapp; mit dem Gerätecode vom Etikett
   anmelden. Kann das Captive-Portal-Fenster des Smartphones die Feldapp nicht
   ausführen, `http://192.168.4.1/` im Browser öffnen oder die einfache
   Einrichtungsseite `http://192.168.4.1/setup` verwenden.
4. Unter **Gerät → Verbindungen** WLAN und NTRIP einrichten, die Änderungen
   prüfen und speichern.
5. Unter **Messen → Feldeinrichtung** Antennenhöhe und Bezugsrahmen des
   Korrekturdienstes eintragen.
6. Nach dem Verbinden `http://<hostname>.local/` öffnen, zum Beispiel
   `http://rtk-a1b2c3.local/`. Wird `.local` von Router oder Hotspot nicht
   aufgelöst, stattdessen die aktuelle IP-Adresse verwenden.

Das Portal arbeitet lokal und ohne Cloud. Es verwendet HTTP, nicht HTTPS.
Deutsch oder Polnisch wird bei einem deutsch- bzw. polnischsprachigen Browser
automatisch gewählt, sonst Englisch; die Sprache lässt sich im Portal
umstellen. Unter **Gerät → Bedienung & Anzeige** lässt sich die Darstellung
wählen: Automatisch (folgt Smartphone oder Computer), Hell, Dunkel oder Hoher
Kontrast. Auf den Seiten Messen und Gerät
kommen die Live-Daten über einen ständig offenen Datenstrom statt durch
wiederholte Abfragen.

## Messen im Feld

Das Portal führt über drei Bereiche: **Messen** · **Projekte** · **Gerät**.
**Messen** beantwortet, ob eine Messung gerade zulässig ist und warum nicht,
und zeigt das aktive Projekt und eine Kurzfassung der Feldeinrichtung (diese
selbst liegt unter **Messen → Feldeinrichtung**). **Projekte** verwaltet
Projekte, Linien, Objektpunkte, Karte, Export und die Archivansicht. **Gerät**
hat vier Bereiche: **Verbindungen** (Internet & Korrekturen, Gerätezugriff),
**Bedienung & Anzeige**, **Diagnose** und **Wartung** (Konfigurationssicherung,
Wiederherstellen und Neustart).

Vor einer präzisen Messung:

1. Antenne stabil, senkrecht und mit freier Sicht zum Himmel aufstellen.
2. Auf **RTK FIXED** und die erteilte Messfreigabe warten.
3. Projekt auswählen oder anlegen und eine Linie starten.
4. Unter **Messen → Feldeinrichtung** Sofortmessung, Mittelung über 5 oder
   10 Messungen oder **bis Messwerte OK** wählen; die Messmethode gilt
   geräteweit, in jedem Browser.
5. Die Qualitätswerte des aufgenommenen Punktes prüfen und das Projekt
   sichern.

Ab Werk verlangt die Messfreigabe einen RTK-FIXED-Fix, mindestens
10 Satelliten, HDOP höchstens 1,5, höchstens 5,0 Sekunden alte Korrekturen,
höchstens 0,05 m Streuung der Einzelmessungen sowie GST-Standardabweichungen
von höchstens 0,05 m horizontal und 0,10 m vertikal. Die Betriebsart „bis
Messwerte OK" bewertet ein gleitendes Fenster aus 5 Messungen. Alle Grenzwerte
sind im angemeldeten Portal (**Messen → Feldeinrichtung → Messregeln**) änderbar und auf die Werkswerte zurücksetzbar.

### Genauigkeit und Datenbasis

Seit V15.9.0 (erstmals veröffentlicht mit V16.0.0) werden Positionen als
exakte Ganzzahlen (1e-8 Grad, Höhen in
Millimetern) aus dem Text des Empfängers gespeichert und in Sicherungen
geschrieben; GeoJSON und CSV geben dieselben Werte als exakte Dezimalgrad und
Meter aus. Der Grund ist, dass der Standard-Float von MicroPython nur 32 Bit
breit ist und einen Breitengrad nur auf etwa 0,4 m auflöst. Punkte, die eine ältere Firmware aufgenommen hat,
werden beim Laden umgewandelt, als `legacy_float32` markiert und bleiben auf
diesem groben Raster; wo die Position zählt, neu messen. Dateien, die
V15.9.0 geschrieben hat, kann eine ältere Firmware nicht lesen; vor einem
Downgrade sichern.

Jeder Fix trägt sein Alter, und ein veralteter Fix wird für Messungen
abgelehnt. Die Korrekturdaten haben drei verschiedene Alter (Eingangsalter,
Epochenalter und das GGA-Korrekturalter des Empfängers), und der Empfänger
kann bei alten Korrekturen weiter RTK FIXED melden: einen Punkt daher nach dem
Urteil der Messfreigabe und den gespeicherten Qualitätswerten beurteilen, nicht
nach der Fix-Anzeige. Die gespeicherte Höhe ist die Höhe des Phasenzentrums der
Antenne über dem mittleren Meeresniveau; vor dem Messen in der Feldeinrichtung Antennenhöhe und
Bezugsrahmen des Korrekturdienstes eintragen und pro Projekt einen Rahmen
verwenden. Aufnahmezeiten zählen ab dem Jahr 2000 und werden mit einer
abgeleiteten UTC-Zeit exportiert. Die übrigen Grenzen stehen in den
[Known limitations](docs/known-limitations.md) (englisch).

### Absteckung

Ein Projekt öffnen und dann **Abstecken** wählen, unter der Projektkarte oder
über die Schaltfläche auf der Karte. Eine GeoJSON-Datei einlesen: Der Browser
prüft sie und zeigt eine Vorschau, in der Teile und Stützpunkte ausgewählt
werden; nur die gewählten Ziele gehen an das Gerät, das sie erneut prüft (bis
zu 4 Sätze je Projekt, 500 Ziele je Satz, 128 KiB je Import). Die Führung
braucht das aktive Projekt (**Zum Abstecken aktivieren**) und einen Fix: Die
Suchansicht zeigt eine genordete Karte, innerhalb des Feinradius (ab Werk 5 m)
zeigt die Feinansicht eine Zielscheibe mit der Gehrichtung, einmal pro Sekunde
aktualisiert. Ein Ziel bestätigen, wenn das Gerät es innerhalb der Toleranz
(ab Werk 0,1 m) als bereit meldet, oder als **Vor Ort gefunden** erfassen;
Ziele lassen sich auch überspringen und wieder öffnen. Jede Bestätigung wird
als Messpunkt vom Typ „Absteckkontrolle“ gespeichert, und die
Absteckübersicht exportiert den Stand aller Ziele als GeoJSON oder CSV.

Das Gerät hat keinen Kompass, Karte und Zielscheibe sind daher immer genordet.
Absteck-Sätze sind nicht Teil der Messdaten-Sicherung; die ursprünglichen
GeoJSON-Dateien aufbewahren.

## Hilfe und Wiederherstellung

- **Portal nicht erreichbar:** WLAN des Smartphones prüfen, `.local` durch die
  aktuelle IP-Adresse ersetzen oder das Einrichtungs-WLAN erzwingen.
- **Einrichtungsmodus:** RST drücken und **BOOT drei Sekunden** halten. Wird
  BOOT schon beim Einschalten gehalten, startet der Chip stattdessen im
  ROM-Bootloader und die Firmware läuft gar nicht.
- **Keine Messfreigabe:** die Checkliste auf der Seite **Messen** von oben nach
  unten abarbeiten oder „Warum nicht?“ öffnen, besonders Korrekturstrom, RTK FIXED, ruhige Antenne und GST.
- **Bluetooth verbindet nicht:** die vorhandene Kopplung auf dem Smartphone
  löschen und mit der BLE-PIN erneut koppeln.
- **Werksreset:** **BOOT zehn Sekunden** halten. WLAN- und NTRIP-Zugangsdaten,
  die alten Token und die BLE-Bonds werden gelöscht, der TCP-Streamtoken wird
  erneuert. **Punktespeicher und archivierte Projekte werden bei einem
  Werksreset nicht gelöscht** – Projekte vor der Weitergabe eines Geräts im
  Portal löschen. Die Geräteidentität und damit Gerätename, Hostname und
  Gerätecode bleiben erhalten.

Sicherungen enthalten WLAN- und NTRIP-Zugangsdaten im Klartext. Sie gehören
nicht in öffentliche Ablagen oder Support-Tickets.

## Verwandte Projekte

Andere offene Projekte lösen verwandte Aufgaben und passen je nach Ziel
vielleicht besser. ESP-RTK ist mit keinem davon verbunden.

- [esp32-gps](https://github.com/mrichar1/esp32-gps): ebenfalls MicroPython
  mit ESP32 und LC29H, aber ohne Webportal. Schwerpunkt ist das Weiterleiten
  von Daten (NTRIP-Client, -Server und -Caster, BLE, ESP-Now), etwa für eine
  Basisstation ohne Bedienung.
- [SparkFun RTK Everywhere](https://github.com/sparkfun/SparkFun_RTK_Everywhere_Firmware):
  Firmware für die fertigen RTK-Geräte von SparkFun.
- [ESP32-XBee](https://github.com/nebkat/esp32-xbee): WLAN-Brücke für
  ESP32-Module im XBee-Format, zum Beispiel den ArduSimple WiFi NTRIP Master.
- [ESPrtk](https://esprtk.com/): kommerzielle ESP32-Platine für
  RTK-Funkstrecken. Trotz des ähnlichen Namens ein eigenes Projekt.

## Lizenz

ESP-RTK steht unter der GNU Affero General Public License, Version 3
(`AGPL-3.0-only`). Der Lizenztext steht in [LICENSE](LICENSE), Hinweise zu
eingebundenem Fremdcode in [THIRD_PARTY.md](THIRD_PARTY.md).
