-- Create the database (run once)
CREATE DATABASE IF NOT EXISTS job_tracker;

-- Use the database
USE job_tracker;

-- Create the jobs table
CREATE TABLE IF NOT EXISTS jobs (
  id INT AUTO_INCREMENT PRIMARY KEY,
  company VARCHAR(255) NOT NULL,
  role VARCHAR(255) NOT NULL,
  location VARCHAR(255) NOT NULL,
  job_link TEXT NOT NULL,
  status VARCHAR(50) NOT NULL,
  applied_date DATE NOT NULL,
  notes TEXT NULL,

  -- Legacy interview columns (kept for backward compatibility; new data goes to interviews table)
  interview_date DATE NULL,
  interview_time TIME NULL,
  interview_venue VARCHAR(255) NULL,
  interview_completed TINYINT(1) NOT NULL DEFAULT 0,
  interview_difficulty VARCHAR(50) NULL,
  interview_experience_notes TEXT NULL
);

-- Dedicated interviews table (stores interview data + company tagging)
CREATE TABLE IF NOT EXISTS interviews (
  id INT AUTO_INCREMENT PRIMARY KEY,
  job_id INT NOT NULL,
  company_tag VARCHAR(255) NOT NULL,
  role_tag VARCHAR(255) NOT NULL,
  interview_date DATE NOT NULL,
  interview_time TIME NOT NULL,
  interview_venue VARCHAR(255) NOT NULL,
  interview_completed TINYINT(1) NOT NULL DEFAULT 0,
  interview_difficulty VARCHAR(50) NULL,
  interview_experience_notes TEXT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_interviews_job (job_id),
  CONSTRAINT fk_interviews_job FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

-- Optional: enforce allowed values (uncomment if you prefer a strict ENUM)
-- ALTER TABLE jobs MODIFY status ENUM('Applied','Interview','Rejected','Offer') NOT NULL;


