# 🚋 TramTram Bot

Bot Telegram per il monitoraggio in tempo reale dei mezzi pubblici di Torino (GTT) tramite API OpenTripPlanner (Muoversi a Torino).

## Funzionalità

- **Cruscotto live** – Messaggio Telegram aggiornato automaticamente ogni 60 secondi con i prossimi arrivi configurati.
- **`/fermata <id>`** – Mostra tutti i mezzi in arrivo a una fermata GTT, aggiornato live con bottone **STOP** per chiudere.
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
git clone https://github.com/tuo-utente/tramtram.git
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
  "polling_interval_seconds": 60,
  "night_pause": { "start_hour": 2, "end_hour": 7 },
  "viaggi": [
    {
      "nome": "Casa → Ufficio",
      "combo": [
        {
          "nome": "Diretto 42",
          "tratte": [
            {
              "linea": "42",
              "stop_id_salita": "1132",
              "stop_id_discesa": "40"
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
| `polling_interval_seconds` | Intervallo di aggiornamento (default: 60) |
| `night_pause.start_hour` | Inizio pausa notturna (default: 2) |
| `night_pause.end_hour` | Fine pausa notturna (default: 7) |
| `viaggi` | Lista dei viaggi da monitorare |

### Struttura dei viaggi

```
Viaggio (es. "Casa → Ufficio")
 └── Combo (es. "Diretto 42", "Cambio 16+4")
      └── Tratta
           ├── linea            (es. "42")
           ├── stop_id_salita   (fermata da monitorare)
           └── stop_id_discesa  (fermata di arrivo, per il nome destinazione)
```

Per trovare gli `stop_id` GTT: usa `/fermata <numero>` nel bot, oppure cerca su [Muoversi a Torino](https://www.muoversiatorino.it/).

## Utilizzo

```bash
python main.py
```

### Comandi Telegram

| Comando | Descrizione |
|---|---|
| `/start` | Crea il cruscotto viaggi con aggiornamento automatico |
| `/refresh` | Forza un aggiornamento immediato del cruscotto |
| `/fermata <id>` | Mostra tutti i mezzi in arrivo alla fermata, con bottone STOP |

### Bottone STOP

I messaggi `/fermata` includono un bottone inline **🛑 STOP**. Premendolo il messaggio viene cancellato dalla chat e rimosso dal tracciamento.

## Esempio di output

### Cruscotto

```
🚋 TramTram – GTT Torino
⏱ Aggiornato: 08:32:15

Casa → Ufficio
  ┣ Diretto 42
  ┃  42 OSPEDALE MAURIZIANO→PORTA NUOVA: 🟢3', 🟢15', 30'
```

### /fermata

```
🚏 Fermata: PORTA NUOVA (40)
⏱ Aggiornato: 08:32:15

  42 → SASSI: 🟢5', 🟢18', 32'
  66 → LINGOTTO: 🟢2', 12'
  4  → FALCHERA: 🟢8'

[🛑 STOP]
```