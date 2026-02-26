# 🚋 TramTram Bot

[English version below](#english-version)

Bot Telegram per il monitoraggio in tempo reale dei mezzi pubblici di Torino (GTT) tramite API OpenTripPlanner (Muoversi a Torino).

## Funzionalità

- **Cruscotto live** – Un messaggio Telegram per ogni viaggio, aggiornato automaticamente ogni 15 secondi con i prossimi arrivi configurati.
- **Fermata rapida** – Invia un numero (stop ID) in chat per vedere tutti i mezzi in arrivo, aggiornato live per 15 minuti con bottone **STOP** per chiudere.
- **Wizard configurazione** – Comandi `/aggiungi` e `/elimina` per gestire viaggi e combo direttamente da Telegram, senza toccare i file.
- **Stato persistente** – Tutti i messaggi attivi vengono salvati in `state.json` e sopravvivono ai riavvii. All'avvio i messaggi precedenti vengono cancellati (niente orfani in chat).
- **Viaggi & Combo** – Configura viaggi multi-tratta con fermata di salita e discesa.
- **Nomi automatici** – Nomi fermata e destinazioni (headsign) derivati dall'API.
- **Pausa notturna** – Nessuna chiamata API nell'intervallo configurato (default 02:00–07:00).

## Struttura

```
tramtram/
├── main.py              # Bot principale
├── config.json          # Configurazione (git-ignored)
├── requirements.txt     # Dipendenze Python
├── state.json           # Stato persistente (auto-generato, git-ignored)
├── README.md
└── .gitignore
```

## Requisiti

- Python 3.10+
- Un bot Telegram (creato via [@BotFather](https://t.me/BotFather))

## Installazione

```bash
# 1. Clona il repository
git clone https://github.com/lucaosti/tramtram.git
cd tramtram

# 2. Crea e attiva il virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Installa le dipendenze
pip install -r requirements.txt

# 4. Crea config.json (vedi sezione Configurazione)
```

## Configurazione (`config.json`)

```json
{
  "telegram": {
    "bot_token": "TOKEN_DA_BOTFATHER",
    "chat_id": 123456789
  },
  "otp_base_url": "https://plan.muoversiatorino.it/otp/routers/mato/index",
  "polling_interval_seconds": 15,
  "night_pause": { "start_hour": 2, "end_hour": 7 },
  "trips": [
    {
      "name": "Casa → Ufficio",
      "combos": [
        {
          "name": "Diretto 42",
          "legs": [
            {
              "line": "42",
              "stop_id_boarding": "1132",
              "stop_id_alighting": "40"
            }
          ]
        }
      ]
    }
  ]
}
```

| Campo | Descrizione |
|---|---|
| `telegram.bot_token` | Token del bot da BotFather |
| `telegram.chat_id` | ID della chat dove inviare i messaggi |
| `otp_base_url` | URL base API OTP Muoversi a Torino |
| `polling_interval_seconds` | Intervallo di aggiornamento in secondi (default: 15) |
| `night_pause.start_hour` | Inizio pausa notturna (default: 2) |
| `night_pause.end_hour` | Fine pausa notturna (default: 7) |
| `trips` | Lista dei viaggi da monitorare |

### Struttura dei viaggi

```
Trip (es. "Casa → Ufficio")
 └── Combo (es. "Diretto 42", "Combo 16 + 4")
      └── Leg
           ├── line                (es. "42")
           ├── stop_id_boarding    (fermata di salita)
           └── stop_id_alighting   (fermata di discesa)
```

Per trovare gli `stop_id` GTT: invia il numero della fermata al bot, oppure cerca su [Muoversi a Torino](https://www.muoversiatorino.it/).

## Utilizzo

```bash
python main.py
```

### Comandi Telegram

| Comando | Descrizione |
|---|---|
| `/start` | Crea il cruscotto viaggi con aggiornamento automatico |
| `/refresh` | Forza un aggiornamento immediato del cruscotto |
| `<numero>` | Invia un numero per vedere tutti i mezzi in arrivo alla fermata (15 min, con bottone STOP) |
| `/aggiungi` | Wizard per aggiungere un nuovo viaggio o combo |
| `/elimina` | Wizard per eliminare un viaggio o una combo |
| `/annulla` | Annulla il wizard in corso |

### Bottone STOP

I messaggi fermata includono un bottone inline **🛑 STOP**. Premendolo il messaggio viene cancellato dalla chat e rimosso dal tracciamento.

## Esempio di output

### Cruscotto

```
🚋  Ufficio → Casa
⏱  08:32:15

━━━  Diretto 42  ━━━

  🚌  42
        OSPEDALE MAURIZIANO  ➜  PORTA NUOVA
        ⏳  🟢3'   🟢15'   30'
```

### Fermata rapida

```
🚏  PORTA NUOVA  (40)
⏱  08:32:15
⏳  scade tra 14 min

  🚌  42  ➜  SASSI
        ⏳  🟢5'   🟢18'   32'

  🚌  66  ➜  LINGOTTO
        ⏳  🟢2'   12'

  🚌  4  ➜  FALCHERA
        ⏳  🟢8'

[🛑 STOP]
```

---

# English version

## 🚋 TramTram Bot (English)

Telegram bot for real-time monitoring of Turin public transport (GTT) via OpenTripPlanner API (Muoversi a Torino).

### Features

- **Live dashboard** – One Telegram message per trip, updated every 15 seconds with upcoming arrivals.
- **Quick stop info** – Send a stop ID (number) in chat to see all arrivals, updated live for 15 minutes with a **STOP** button to close.
- **Config wizard** – Use `/aggiungi` and `/elimina` to manage trips and combos directly from Telegram, no file editing needed.
- **Persistent state** – All active messages are saved in `state.json` and survive restarts. On startup, previous messages are deleted (no orphaned chat messages).
- **Trips & Combos** – Configure multi-leg trips with boarding and alighting stops.
- **Automatic names** – Stop and destination names (headsign) are fetched from the API.
- **Night pause** – No API calls during the configured interval (default 02:00–07:00).

### Structure

```
tramtram/
├── main.py              # Main bot
├── config.json          # Configuration (git-ignored)
├── requirements.txt     # Python dependencies
├── state.json           # Persistent state (auto-generated, git-ignored)
├── README.md
└── .gitignore
```

### Requirements

- Python 3.10+
- A Telegram bot (created via [@BotFather](https://t.me/BotFather))

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/lucaosti/tramtram.git
cd tramtram

# 2. Create and activate the virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create config.json (see Configuration section)
```

### Configuration (`config.json`)

```json
{
  "telegram": {
    "bot_token": "TOKEN_FROM_BOTFATHER",
    "chat_id": 123456789
  },
  "otp_base_url": "https://plan.muoversiatorino.it/otp/routers/mato/index",
  "polling_interval_seconds": 15,
  "night_pause": { "start_hour": 2, "end_hour": 7 },
  "trips": [
    {
      "name": "Home → Office",
      "combos": [
        {
          "name": "Direct 42",
          "legs": [
            {
              "line": "42",
              "stop_id_boarding": "1132",
              "stop_id_alighting": "40"
            }
          ]
        }
      ]
    }
  ]
}
```

| Field | Description |
|---|---|
| `telegram.bot_token` | Bot token from BotFather |
| `telegram.chat_id` | Chat ID to send messages |
| `otp_base_url` | Base API URL for OTP Muoversi a Torino |
| `polling_interval_seconds` | Update interval in seconds (default: 15) |
| `night_pause.start_hour` | Night pause start (default: 2) |
| `night_pause.end_hour` | Night pause end (default: 7) |
| `trips` | List of trips to monitor |

#### Trip structure

```
Trip (e.g. "Home → Office")
 └── Combo (e.g. "Direct 42", "Combo 16 + 4")
      └── Leg
           ├── line                (e.g. "42")
           ├── stop_id_boarding    (boarding stop)
           └── stop_id_alighting   (alighting stop)
```

To find GTT `stop_id`: send the stop number to the bot, or search on [Muoversi a Torino](https://www.muoversiatorino.it/).

### Usage

```bash
python main.py
```

#### Telegram commands

| Command | Description |
|---|---|
| `/start` | Create the dashboard with automatic updates |
| `/refresh` | Force an immediate dashboard update |
| `<number>` | Send a number to see all arrivals at that stop (15 min, with STOP button) |
| `/aggiungi` | Wizard to add a new trip or combo |
| `/elimina` | Wizard to delete a trip or combo |
| `/annulla` | Cancel the current wizard |

#### STOP button

Stop messages include an inline **🛑 STOP** button. Pressing it deletes the message from chat and removes it from tracking.

### Example output

##### Dashboard

```
🚋  Office → Home
⏱  08:32:15

━━━  Direct 42  ━━━

  🚌  42
        OSPEDALE MAURIZIANO  ➜  PORTA NUOVA
        ⏳  🟢3'   🟢15'   30'
```

##### Quick stop info

```
🚏  PORTA NUOVA  (40)
⏱  08:32:15
⏳  expires in 14 min

  🚌  42  ➜  SASSI
        ⏳  🟢5'   🟢18'   32'

  🚌  66  ➜  LINGOTTO
        ⏳  🟢2'   12'

  🚌  4  ➜  FALCHERA
        ⏳  🟢8'

[🛑 STOP]
```