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

## Setup

### Backend Setup

1. Navigate to the backend directory:
   ```bash
   cd oasis_backend
   ```

2. Create a virtual environment (optional but recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run migrations:
   ```bash
   python manage.py makemigrations
   python manage.py migrate
   ```

5. Start the Django development server:
   ```bash
   python manage.py runserver
   ```

### Frontend Setup

1. Navigate to the frontend directory:
   ```bash
   cd oasis_frontend
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