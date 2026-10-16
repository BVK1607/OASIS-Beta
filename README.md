# OASIS - Optimised Analytics Systems for Investment Solutions

OASIS is a full-stack web application for portfolio management and investment analytics. It provides tools for portfolio tracking, performance analysis, and investment learning resources.

## Features

- Portfolio Management
  - Upload holdings via CSV
  - Manual entry of holdings
  - Real-time market data integration
- Analytics
  - Performance metrics
  - Risk analysis
  - Factor analysis
- Learning Resources
  - Investment education materials
  - Market insights

## Tech Stack

- Backend: Django + Django REST Framework
- Frontend: React + Material-UI
- Data: yfinance API for market data

## Project Structure

```
backend/     Django project (API, models, management commands)
  backend/   Settings and root URLs
  portfolio/ Holdings, NAV, returns, market data
  learning/  Learning resources
  analytics/ Analytics app (placeholder)
frontend/    React app (git submodule, see note below)
```

## Setup

### Backend

1. Go to the backend directory:
   ```bash
   cd backend
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Apply migrations (they're already committed, so no `makemigrations` needed):
   ```bash
   python manage.py migrate
   ```

5. Start the development server:
   ```bash
   python manage.py runserver
   ```

#### Environment variables

All are optional for local development.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | dev-only placeholder | **Set this in any deployed environment** |
| `DJANGO_DEBUG` | `True` | Set to `False` in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated hostnames |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Comma-separated frontend origins |
| `REDIS_URL` | unset (in-memory cache) | e.g. `redis://127.0.0.1:6379/1` to use Redis |

#### Running tests

```bash
python manage.py test
```

Some tests call Yahoo Finance and can fail when it rate-limits requests.

#### Scheduled jobs

`backend/crontab` has example cron entries for refreshing NAV and benchmark data. Replace `/path/to/backend` with your actual path.

### Frontend

The frontend lives in a separate repo ([OASIS-Final](https://github.com/BVK1607/OASIS-Final)) and is included as a git submodule. Clone with:

```bash
git clone --recurse-submodules https://github.com/BVK1607/OASIS-Beta.git
```

or, in an existing clone:

```bash
git submodule update --init
```

1. Go to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Start the development server:
   ```bash
   npm start
   ```

## Usage

1. Access the application at `http://localhost:3000`
2. Use the navigation bar to switch between different features
3. In the Portfolio section:
   - Upload holdings using a CSV file with columns: ISIN, Ticker, Quantity, Book Cost, Currency, Trade Date
   - Or manually add holdings using the "Add Holding" button
4. View real-time market data and portfolio analytics in the Analytics section

## Development

- Backend API runs on `http://localhost:8000`
- Frontend development server runs on `http://localhost:3000`
- API endpoints are prefixed with `/api/`

## CSV Format

The CSV file for uploading holdings should have the following columns:
- ISIN
- Ticker
- Quantity
- Book Cost
- Currency
- Trade Date (YYYY-MM-DD format)
