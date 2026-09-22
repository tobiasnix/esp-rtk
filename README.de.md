# ESP-RTK – Kurzanleitung

ESP-RTK ist eine MicroPython-Firmware für ein kompaktes RTK-Feldgerät aus
ESP32-S3 und Quectel-LC29H-Empfänger. Es empfängt NTRIP-Korrekturdaten über
WLAN, zeigt die Messqualität im lokalen Webportal und gibt NMEA über
Bluetooth LE oder TCP aus. Projekte, Linien und Objektpunkte werden auf dem
Gerät gespeichert und als GeoJSON, CSV oder Sicherung exportiert.

Aktuelle Firmware: **V15.8.0**

Diese Seite ist die deutsche Kurzanleitung für Anwenderinnen und Anwender.
Installation, Hardware, Schnittstellen und alle technischen Angaben stehen in
der englischen Dokumentation, beginnend mit [README.md](README.md).

## In fünf Schritten starten

1. Gerät einschalten und den Start abwarten.
2. Den WLAN-QR-Code auf dem Geräteetikett scannen. Ist das Einrichtungs-WLAN
   nicht sichtbar, RST drücken und danach die **BOOT-Taste drei Sekunden**
   halten. Das Einrichtungsnetz heißt nach dem Gerät, zum Beispiel
   `RTK-A1B2C3-SETUP`, und das Portal antwortet unter `http://192.168.4.1/`.
3. Das Portal öffnen und mit dem Gerätecode vom Etikett anmelden.
4. Unter **Gerät** WLAN und NTRIP einrichten und speichern.
5. Nach dem Verbinden `http://<hostname>.local/` öffnen, zum Beispiel
   `http://rtk-a1b2c3.local/`. Wird `.local` von Router oder Hotspot nicht
   aufgelöst, stattdessen die aktuelle IP-Adresse verwenden.

Das Portal arbeitet lokal und ohne Cloud. Es verwendet HTTP, nicht HTTPS.
Deutsch wird bei einem deutschsprachigen Browser automatisch gewählt; die
Sprache lässt sich im Portal umstellen.

## Messen im Feld

Das Portal führt über drei Bereiche: **Status** beantwortet, ob eine Messung
gerade zulässig ist und warum nicht. **Projekte** verwaltet Projekte, Linien,
Objektpunkte, Karte und Export. **Gerät** enthält Verbindungen, Konfiguration,
Protokoll und Diagnose.

Vor einer präzisen Messung:

1. Antenne stabil, senkrecht und mit freier Sicht zum Himmel aufstellen.
2. Auf **RTK FIXED** und die erteilte Messfreigabe warten.
3. Projekt auswählen oder anlegen und eine Linie starten.
4. Sofortmessung, Mittelung über 5 oder 10 Messungen oder **bis Messwerte
   OK** wählen.
5. Die Qualitätswerte des aufgenommenen Punktes prüfen und das Projekt
   sichern.

Ab Werk verlangt die Messfreigabe einen RTK-FIXED-Fix, mindestens
10 Satelliten, HDOP höchstens 1,5, höchstens 5,0 Sekunden alte Korrekturen,
höchstens 0,05 m Streuung der Einzelmessungen sowie GST-Standardabweichungen
von höchstens 0,05 m horizontal und 0,10 m vertikal. Die Betriebsart „bis
Messwerte OK" bewertet ein gleitendes Fenster aus 5 Messungen. Alle Grenzwerte
sind im angemeldeten Portal änderbar und auf die Werkswerte zurücksetzbar.

## Hilfe und Wiederherstellung

- **Portal nicht erreichbar:** WLAN des Smartphones prüfen, `.local` durch die
  aktuelle IP-Adresse ersetzen oder das Einrichtungs-WLAN erzwingen.
- **Einrichtungsmodus:** RST drücken und **BOOT drei Sekunden** halten. Wird
  BOOT schon beim Einschalten gehalten, startet der Chip stattdessen im
  ROM-Bootloader und die Firmware läuft gar nicht.
- **Keine Messfreigabe:** die Hinweise im Statusbildschirm von oben nach unten
  abarbeiten, besonders Korrekturstrom, RTK FIXED, ruhige Antenne und GST.
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

## Lizenz

ESP-RTK steht unter der GNU Affero General Public License, Version 3
(`AGPL-3.0-only`). Der Lizenztext steht in [LICENSE](LICENSE), Hinweise zu
eingebundenem Fremdcode in [THIRD_PARTY.md](THIRD_PARTY.md).
