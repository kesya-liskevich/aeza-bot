CREATE TYPE ticket_status AS ENUM ('new','in_progress','wait_client','closed','escalated');

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  tg_id VARCHAR(64) UNIQUE NOT NULL,
  name VARCHAR(255) NOT NULL,
  phone VARCHAR(64),
  company VARCHAR(255),
  lang VARCHAR(8) DEFAULT 'ru',
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE managers (
  id SERIAL PRIMARY KEY,
  tg_id VARCHAR(64) UNIQUE NOT NULL,
  name VARCHAR(255) NOT NULL,
  role VARCHAR(32) DEFAULT 'manager',
  skills JSONB DEFAULT '{}'::jsonb,
  load_score INT DEFAULT 0,
  is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE tickets (
  id SERIAL PRIMARY KEY,
  user_id INT REFERENCES users(id),
  status ticket_status NOT NULL DEFAULT 'new',
  topic VARCHAR(128) NOT NULL,
  route_from VARCHAR(128) NOT NULL,
  route_to VARCHAR(128) NOT NULL,
  weight_kg INT,
  volume_cbm REAL,
  pallets INT,
  incoterms VARCHAR(16),
  date_ready TIMESTAMP,
  date_delivery TIMESTAMP,
  assigned_manager_id INT REFERENCES managers(id),
  price_hint_min INT,
  price_hint_max INT,
  eta_days INT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_tickets_status ON tickets(status);
CREATE INDEX idx_tickets_assignee ON tickets(assigned_manager_id);

CREATE TABLE messages (
  id SERIAL PRIMARY KEY,
  ticket_id INT REFERENCES tickets(id),
  from_role VARCHAR(16) NOT NULL,
  from_id VARCHAR(64) NOT NULL,
  tg_message_id VARCHAR(64),
  text VARCHAR(4096) NOT NULL,
  attachments JSONB,
  created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_messages_ticket ON messages(ticket_id);
