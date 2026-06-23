# Cheap Flights AI Agent

A prompt-driven flight search app that finds and ranks live fares, supports
flexible dates and multi-city stopovers, and lets travelers refine results in a
conversation.

## Features

- Live Google Flights results through SerpApi
- Natural-language trip and follow-up interpretation
- Optional OpenAI Structured Outputs integration
- Flexible-date searches with hard budget enforcement
- Multi-city and return-stopover itineraries
- PostgreSQL airport, city, country, alias, and map-coordinate catalog
- Interactive route map
- Ranked fare cards and natural-language recommendations
- Follow-up comparisons, trip edits, undo, and saved browser chats

## Run With Docker

Requirements:

- Docker Desktop
- A [SerpApi](https://serpapi.com/) API key
- Optional [OpenAI API](https://platform.openai.com/api-keys) key

1. Clone the repository and create the environment file:

   ```bash
   cp .env.example .env
   ```

   On Windows PowerShell:

   ```powershell
   Copy-Item .env.example .env
   ```

2. Add your API keys to `.env`:

   ```text
   SERPAPI_API_KEY=your-serpapi-key
   OPENAI_API_KEY=your-openai-api-key
   OPENAI_MODEL=gpt-5.5
   ```

   `OPENAI_API_KEY` is optional. Without it, the app uses its local parser.

3. Build and start the complete stack:

   ```bash
   docker compose up --build
   ```

4. Open [http://localhost:8000](http://localhost:8000).

The first startup downloads the public OurAirports dataset and imports it into
PostgreSQL. Later starts reuse the named Docker volume.

Stop the stack:

```bash
docker compose down
```

Remove the database volume as well:

```bash
docker compose down -v
```

## Local Development

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up -d postgres
.\venv\Scripts\python.exe -m cheap_flights_agent.import_locations
.\venv\Scripts\python.exe -m cheap_flights_agent.web
```

The local Python process uses the `DATABASE_URL` from `.env`, mapped to Docker
PostgreSQL on port `5433`.

Run tests:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests
```

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `SERPAPI_API_KEY` | Yes for live fares | SerpApi credential |
| `DATABASE_URL` | Local development | PostgreSQL connection string |
| `OPENAI_API_KEY` | No | Enables LLM prompt interpretation |
| `OPENAI_MODEL` | No | Defaults to `gpt-5.5` |

Never commit `.env` or API keys.

## Architecture

[View the high-level architecture diagram](docs/architecture.md).

- `agent.py`: orchestration, fallback parsing, and natural-language summaries
- `providers.py`: SerpApi flight and flexible-date integration
- `llm.py`: OpenAI Structured Outputs interpretation
- `locations.py`: PostgreSQL location repository
- `import_locations.py`: OurAirports catalog importer
- `web.py`: local HTTP server and JSON endpoints
- `web_assets/`: prompt UI, map, result cards, and chat history

## Data Sources

- Flight results: [SerpApi Google Flights API](https://serpapi.com/google-flights-api)
- Airport catalog: [OurAirports data](https://ourairports.com/data/)
- Map tiles: [OpenStreetMap](https://www.openstreetmap.org/)

## License

[MIT](LICENSE)
