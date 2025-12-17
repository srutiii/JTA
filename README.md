# Job Application Tracker (Flask + MySQL)

Minimal, beginner-friendly **Job Application Tracker** built with:

- **Backend**: Python Flask
- **Database**: MySQL
- **Frontend**: HTML + CSS (Bootstrap, no heavy JavaScript)

## Features

- **Add jobs** (Job Link is mandatory + validated)
- **Applied follow-up reminder**: flags jobs that stayed in **Applied** status for **3+ days**
- **Edit jobs / update status**
- **Interviews workflow**
  - When a job is set to **Interview**, you can confirm **date / time / venue**
  - Interview details are stored in a dedicated **`interviews`** table
  - Company is **tagged** using `company_tag` (snapshot of the job’s company)
  - Mark interview completed and store **difficulty + experience notes**
- **Interviews page**: shows **Upcoming** and **Past** interviews with highlights for today/soon

## Project Structure

```
job_tracker/
  app.py
  requirements.txt
  schema.sql
  templates/
  static/
```

## Database Setup (MySQL)

1. Create a MySQL database named **`job_tracker`**.
2. Run the SQL in `job_tracker/schema.sql` (recommended).

Notes:

- The app also tries to create/migrate tables on startup (`ensure_schema()`), but running the SQL once is the cleanest start.
- If you already have an older `interviews` table, the app contains compatibility logic (some legacy schemas use a required `venue` column).

## Configure MySQL Credentials

Edit `DB_CONFIG` in `job_tracker/app.py`:

- `host`
- `user`
- `password`
- `database` (should remain `job_tracker`)

## Run (Windows / PowerShell)

From the repository root:

```powershell
cd job_tracker
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open:

- Jobs: `http://127.0.0.1:5000/`
- Add Job: `http://127.0.0.1:5000/add`
- Interviews: `http://127.0.0.1:5000/interviews`

## Main Routes

- **GET `/`**: list jobs + follow-up reminders
- **GET `/add`**: add job form
- **POST `/add`**: create job
- **GET `/jobs/<id>/edit`**: edit job form
- **POST `/jobs/<id>/edit`**: update job
- **GET/POST `/jobs/<id>/interview/confirm`**: save interview details (writes to `interviews`)
- **GET/POST `/jobs/<id>/interview/complete`**: mark interview completed + save feedback
- **GET `/interviews`**: upcoming/past interviews

## Troubleshooting

- **Can’t connect to MySQL**: confirm MySQL is running, credentials are correct, and database `job_tracker` exists.
- **Port 5000 already in use**: stop the other process using port 5000 or change the port in `app.py` (`app.run(..., port=5001)`).
- **Windows stability note**: the app uses `use_pure=True` for `mysql-connector-python` for better Windows compatibility.


