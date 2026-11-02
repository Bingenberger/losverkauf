# Individueller Verwaltungszugang

`ADMIN_USERNAME` muss den persönlichen Namen der zuständigen Person enthalten;
`ADMIN_PASSWORD` bleibt als bestehender Variablenname erhalten. Beide Werte werden
bei der Anmeldung geprüft. Alte reine `admin=true`-Sitzungen genügen nicht mehr.
Ein Benutzerwechsel erfordert eine neue Anmeldung. Elternzugänge und vorhandene
`edit_token`-Werte werden nicht verändert.

Dies bindet den bisherigen einzelnen Verwaltungszugang an eine Identität. Es ist
noch keine Mehrbenutzerverwaltung mit individuellen Passwörtern und Audit-Trail.
