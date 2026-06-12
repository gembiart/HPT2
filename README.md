# HPT2 - CasparCG Playlist Bridge

Projekt zawiera dwa procesy:

1. `caspar_playlist_daemon.py` - lokalny daemon, który trzyma playlistę, zapisuje ją na dysku i steruje CasparCG Server przez AMCP.
2. `caspar_playlist_client.py` - klient GUI w Python/Tkinter do edycji playlisty i wysyłania zmian do daemona.

Po wysłaniu playlisty klient GUI może zostać zamknięty. Daemon dalej ma listę w pamięci oraz w pliku `playlist_state.json` i steruje CasparCG.

## Wymagania

Python 3.10+.

Opcjonalnie, ale zalecane dla logo PNG z alfą:

```cmd
pip install -r requirements.txt
```

`Pillow` jest używany do przygotowania logo jako pełnoekranowego PNG 1920x1080 z przezroczystym tłem. Dzięki temu małe logo nie powinno pojawiać się z czarnym tłem.

## Uruchomienie

Najpierw uruchom CasparCG Server i sprawdź, czy AMCP działa na porcie 5250.

Potem uruchom daemon:

```cmd
python caspar_playlist_daemon.py --media-dir "c:\Projekty\mxf"
```

Domyślne ustawienia:

- API daemona: `127.0.0.1:8765`
- CasparCG AMCP: `127.0.0.1:5250`
- warstwa video: `1-10`
- warstwa logo: `1-20`
- przejście video: `MIX 50`
- odpytywanie `INFO`: co 2 sekundy

Uruchom GUI:

```cmd
python caspar_playlist_client.py
```

W GUI:

1. Kliknij `Połącz / odśwież`.
2. Dodaj pozycje playlisty ręcznie przyciskiem `Dodaj` albo przyciskiem `Dodaj MXF`.
3. Kliknij `Wyślij całą listę`, jeśli importujesz/układasz listę lokalnie.
4. Kliknij `Start`.
5. Dalsze zmiany typu dodaj/usuń/zmień/przenieś są wysyłane do daemona jako pojedyncze operacje.

Operacje są wykonywane bezpośrednio na liście:

- `Dodaj` wstawia pozycję po zaznaczonym wierszu albo na końcu listy.
- `Dodaj MXF` pozwala wybrać jeden lub wiele plików `.mxf` / `.lxf`.
- `Zmień` albo dwuklik na wierszu otwiera okno zmiany pozycji.
- `Usuń` albo klawisz `Delete` usuwa zaznaczony wiersz.
- `W górę` / `W dół` przenosi zaznaczony wiersz.

Przy `Dodaj MXF` klient bierze nazwę pliku bez rozszerzenia i wysyła do daemona
operacje `insert_item` od pozycji po zaznaczonym wierszu.

## Format pozycji playlisty

Każda pozycja ma:

```json
{
  "id": "stale-id-pozycji",
  "clip": "BRU79145",
  "logo": "abc_000"
}
```

`clip` i `logo` podajesz jak w AMCP, zwykle bez rozszerzenia. Pliki powinny być widoczne dla CasparCG w katalogu `media`.

## Protokół API

Daemon słucha na TCP i używa formatu **JSON Lines**:

- jedna linia = jeden JSON,
- linia kończy się `\n`,
- odpowiedź też jest jednym JSON-em zakończonym `\n`.

Przykład zapytania:

```json
{"id":"1","action":"ping","payload":{}}
```

Przykład odpowiedzi:

```json
{"ok":true,"id":"1","result":{"message":"pong"}}
```

W przypadku błędu:

```json
{"ok":false,"error":"index 10 out of range"}
```

### `replace_playlist`

Wysyła całą listę do daemona.

```json
{
  "id": "replace-1",
  "action": "replace_playlist",
  "payload": {
    "start_index": 0,
    "items": [
      {"clip": "BRU79145", "logo": "abc_000"},
      {"clip": "BRU79146", "logo": "abc_001"}
    ]
  }
}
```

### `insert_item`

Dodaje jedną pozycję na indeks. Indeksy są liczone od zera.

```json
{
  "id": "insert-1",
  "action": "insert_item",
  "payload": {
    "index": 20,
    "item": {"clip": "BRW54970", "logo": "logo_b"}
  }
}
```

### `delete_item`

Usuwa jedną pozycję po indeksie.

```json
{
  "id": "delete-1",
  "action": "delete_item",
  "payload": {
    "index": 10
  }
}
```

### `update_item`

Zmienia pozycję po indeksie.

```json
{
  "id": "update-1",
  "action": "update_item",
  "payload": {
    "index": 5,
    "item": {"clip": "BRU79148", "logo": "logo_c"}
  }
}
```

### `move_item`

Przenosi pozycję.

```json
{
  "id": "move-1",
  "action": "move_item",
  "payload": {
    "old_index": 4,
    "new_index": 12
  }
}
```

### `play`

Start od wybranego indeksu.

```json
{
  "id": "play-1",
  "action": "play",
  "payload": {
    "start_index": 0
  }
}
```

### `stop`

Zatrzymuje logikę playlisty. Opcjonalnie czyści warstwy w CasparCG.

```json
{
  "id": "stop-1",
  "action": "stop",
  "payload": {
    "clear": true
  }
}
```

### `get_state`

Pobiera aktualny stan daemona.

```json
{
  "id": "state-1",
  "action": "get_state",
  "payload": {}
}
```

## Logika odtwarzania

Daemon robi:

1. `PLAY` dla pierwszego/wybranego klipu.
2. Sprawdza `INFO 1-10`.
3. Gdy wie, który klip gra, ładuje następny przez `LOADBG 1-10 "NAZWA" MIX 50 AUTO`.
4. Po przejściu na kolejny klip przesuwa `current_index` i ładuje następny.
5. Zmiany z GUI aktualizują kolejkę w daemonie. Jeśli usuniesz pozycję o indeksie 10, daemon usuwa ją ze swojej kolejki. Jeśli dodasz pozycję na indeks 20, daemon wstawia ją w tym miejscu.

Uwaga: jeśli ten sam plik występuje kilka razy pod rząd, CasparCG w `INFO` zwykle pokazuje tylko nazwę pliku, więc idealne rozróżnienie dwóch identycznych sąsiednich pozycji nie zawsze jest możliwe. Najbezpieczniej używać różnych nazw plików albo nie układać identycznych plików bezpośrednio jeden po drugim.
