package main

import (
	"database/sql"
	"fmt"
	"log"
	"os"
	"strings"
	"text/tabwriter"
	"time"

	_ "github.com/marcboeker/go-duckdb/v2"
)

const schemaSQL = `
CREATE TABLE IF NOT EXISTS contacts (
    jid             VARCHAR PRIMARY KEY,
    push_name       VARCHAR,
    full_name       VARCHAR,
    business_name   VARCHAR,
    is_group        BOOLEAN DEFAULT false,
    group_name      VARCHAR
);

CREATE TABLE IF NOT EXISTS messages (
    id              VARCHAR NOT NULL,
    chat_jid        VARCHAR NOT NULL,
    sender_jid      VARCHAR NOT NULL,
    is_from_me      BOOLEAN NOT NULL,
    is_group        BOOLEAN NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    msg_type        VARCHAR NOT NULL DEFAULT 'text',
    text_content    VARCHAR,
    media_type      VARCHAR,
    push_name       VARCHAR,
    sent_date       DATE NOT NULL,
    sent_hour       UTINYINT NOT NULL,
    sent_dow        UTINYINT NOT NULL,
    raw_proto       BLOB,
    media_downloaded BOOLEAN DEFAULT false,
    media_path      VARCHAR,
    PRIMARY KEY (id, chat_jid)
);
`

// MessageRecord holds all fields needed to persist a single WhatsApp message.
type MessageRecord struct {
	ID          string
	ChatJID     string
	SenderJID   string
	IsFromMe    bool
	IsGroup     bool
	Timestamp   time.Time
	MsgType     string // "text", "image", "video", "audio", "document", "sticker", "location", "contact", "reaction", "other"
	TextContent string // extracted text/caption
	MediaType   string // MIME type if media
	PushName    string // sender's display name
	RawProto    []byte // serialized waE2E.Message protobuf
}

// DownloadableMessage holds the fields needed to re-download media for a message.
type DownloadableMessage struct {
	ID        string
	ChatJID   string
	MsgType   string
	MediaType string
	Timestamp time.Time
	RawProto  []byte
}

// DuckStore wraps a DuckDB database connection and exposes high-level store
// operations for the WhatsApp sync pipeline.
type DuckStore struct {
	db *sql.DB
}

// NewDuckStore opens (or creates) the DuckDB file at path and initialises the
// schema. The caller is responsible for calling Close when done.
func NewDuckStore(path string) (*DuckStore, error) {
	db, err := sql.Open("duckdb", path)
	if err != nil {
		return nil, fmt.Errorf("open duckdb %q: %w", path, err)
	}

	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping duckdb %q: %w", path, err)
	}

	// Load extensions that wactx may have used on this DB (HNSW indexes, property graphs).
	// Errors ignored — extensions may not be available in all environments.
	for _, ext := range []string{"vss", "duckpgq"} {
		db.Exec("INSTALL " + ext)
		db.Exec("LOAD " + ext)
	}

	if _, err := db.Exec(schemaSQL); err != nil {
		db.Close()
		return nil, fmt.Errorf("initialise schema: %w", err)
	}

	// Migrate: add columns that may not exist in older databases.
	migrations := []string{
		"ALTER TABLE messages ADD COLUMN IF NOT EXISTS raw_proto BLOB",
		"ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_downloaded BOOLEAN DEFAULT false",
		"ALTER TABLE messages ADD COLUMN IF NOT EXISTS media_path VARCHAR",
	}
	for _, m := range migrations {
		if _, err := db.Exec(m); err != nil {
			log.Printf("store: migration warning: %v", err)
		}
	}

	log.Printf("store: opened DuckDB at %q", path)
	return &DuckStore{db: db}, nil
}

// InsertMessages persists a batch of MessageRecord values in a single
// transaction. Duplicate (id, chat_jid) pairs are silently ignored via
// INSERT OR IGNORE semantics.
func (s *DuckStore) InsertMessages(records []MessageRecord) error {
	if len(records) == 0 {
		return nil
	}

	tx, err := s.db.Begin()
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	defer func() {
		if err != nil {
			_ = tx.Rollback()
		}
	}()

	const insertSQL = `
INSERT OR IGNORE INTO messages
    (id, chat_jid, sender_jid, is_from_me, is_group, timestamp,
     msg_type, text_content, media_type, push_name,
     sent_date, sent_hour, sent_dow, raw_proto)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`

	stmt, err := tx.Prepare(insertSQL)
	if err != nil {
		return fmt.Errorf("prepare insert statement: %w", err)
	}
	defer stmt.Close()

	inserted := 0
	for _, r := range records {
		ts := r.Timestamp.UTC()
		sentDate := ts.Format("2006-01-02")
		sentHour := uint8(ts.Hour())
		sentDOW := uint8(ts.Weekday()) // 0 = Sunday … 6 = Saturday

		res, err := stmt.Exec(
			r.ID,
			r.ChatJID,
			r.SenderJID,
			r.IsFromMe,
			r.IsGroup,
			ts,
			r.MsgType,
			r.TextContent,
			r.MediaType,
			r.PushName,
			sentDate,
			sentHour,
			sentDOW,
			r.RawProto,
		)
		if err != nil {
			return fmt.Errorf("insert message %q: %w", r.ID, err)
		}

		n, err := res.RowsAffected()
		if err == nil {
			inserted += int(n)
		}
	}

	if err = tx.Commit(); err != nil {
		return fmt.Errorf("commit transaction: %w", err)
	}

	log.Printf("store: inserted %d/%d messages", inserted, len(records))
	return nil
}

// GetHighWatermark returns the maximum timestamp across all stored messages.
// If no messages exist, it returns a zero time.Time and a nil error.
func (s *DuckStore) GetHighWatermark() (time.Time, error) {
	var ts sql.NullTime
	err := s.db.QueryRow("SELECT MAX(timestamp) FROM messages").Scan(&ts)
	if err != nil || !ts.Valid {
		return time.Time{}, err
	}
	return ts.Time, nil
}

// GetMessagesForDownload returns messages that have a raw_proto but have not
// yet had their media downloaded. The result set can be narrowed by time range,
// chat JID, and message types. By default only image/video/audio/document
// messages are returned.
func (s *DuckStore) GetMessagesForDownload(after, before time.Time, chatJID string, types []string) ([]DownloadableMessage, error) {
	// Build the msg_type IN clause.
	msgTypes := types
	if len(msgTypes) == 0 {
		msgTypes = []string{"image", "video", "audio", "document"}
	}
	placeholders := make([]string, len(msgTypes))
	for i := range msgTypes {
		placeholders[i] = "?"
	}
	typeClause := "msg_type IN (" + strings.Join(placeholders, ", ") + ")"

	query := "SELECT id, chat_jid, msg_type, media_type, timestamp, raw_proto FROM messages WHERE raw_proto IS NOT NULL AND NOT media_downloaded AND " + typeClause

	args := make([]any, 0, len(msgTypes)+3)
	for _, t := range msgTypes {
		args = append(args, t)
	}

	if !after.IsZero() {
		query += " AND timestamp >= ?"
		args = append(args, after)
	}
	if !before.IsZero() {
		query += " AND timestamp <= ?"
		args = append(args, before)
	}
	if chatJID != "" {
		query += " AND chat_jid = ?"
		args = append(args, chatJID)
	}

	query += " ORDER BY timestamp ASC"

	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("query messages for download: %w", err)
	}
	defer rows.Close()

	var results []DownloadableMessage
	for rows.Next() {
		var m DownloadableMessage
		var mediaType sql.NullString
		if err := rows.Scan(&m.ID, &m.ChatJID, &m.MsgType, &mediaType, &m.Timestamp, &m.RawProto); err != nil {
			return nil, fmt.Errorf("scan downloadable message: %w", err)
		}
		if mediaType.Valid {
			m.MediaType = mediaType.String
		}
		results = append(results, m)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate downloadable messages: %w", err)
	}

	return results, nil
}

// MarkMediaDownloaded updates the media_downloaded flag and records the local
// path for the given (id, chat_jid) pair.
func (s *DuckStore) MarkMediaDownloaded(id, chatJID, mediaPath string) error {
	_, err := s.db.Exec(
		"UPDATE messages SET media_downloaded = true, media_path = ? WHERE id = ? AND chat_jid = ?",
		mediaPath, id, chatJID,
	)
	return err
}

// UpsertContact inserts or replaces a contact record identified by jid.
func (s *DuckStore) UpsertContact(jid, pushName, fullName, businessName string, isGroup bool, groupName string) error {
	const upsertSQL = `
INSERT OR REPLACE INTO contacts
    (jid, push_name, full_name, business_name, is_group, group_name)
VALUES (?, ?, ?, ?, ?, ?)`

	if _, err := s.db.Exec(upsertSQL, jid, pushName, fullName, businessName, isGroup, groupName); err != nil {
		return fmt.Errorf("upsert contact %q: %w", jid, err)
	}
	return nil
}

// Query executes an arbitrary SQL query and prints the results as a formatted
// table to stdout. It is intended for ad-hoc inspection and debugging.
func (s *DuckStore) Query(query string) error {
	rows, err := s.db.Query(query)
	if err != nil {
		return fmt.Errorf("execute query: %w", err)
	}
	defer rows.Close()

	cols, err := rows.Columns()
	if err != nil {
		return fmt.Errorf("get column names: %w", err)
	}

	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	defer w.Flush()

	// Print header row.
	for i, col := range cols {
		if i > 0 {
			fmt.Fprint(w, "\t")
		}
		fmt.Fprint(w, col)
	}
	fmt.Fprintln(w)

	// Print separator.
	for i, col := range cols {
		if i > 0 {
			fmt.Fprint(w, "\t")
		}
		for range col {
			fmt.Fprint(w, "-")
		}
	}
	fmt.Fprintln(w)

	// Allocate reusable scan destinations.
	values := make([]any, len(cols))
	valuePtrs := make([]any, len(cols))
	for i := range values {
		valuePtrs[i] = &values[i]
	}

	rowCount := 0
	for rows.Next() {
		if err := rows.Scan(valuePtrs...); err != nil {
			return fmt.Errorf("scan row: %w", err)
		}
		for i, val := range values {
			if i > 0 {
				fmt.Fprint(w, "\t")
			}
			if val == nil {
				fmt.Fprint(w, "NULL")
			} else {
				fmt.Fprintf(w, "%v", val)
			}
		}
		fmt.Fprintln(w)
		rowCount++
	}

	if err := rows.Err(); err != nil {
		return fmt.Errorf("iterate rows: %w", err)
	}

	fmt.Fprintf(w, "\n(%d row(s))\n", rowCount)
	return nil
}

// Close releases the underlying database connection.
func (s *DuckStore) Close() error {
	if err := s.db.Close(); err != nil {
		return fmt.Errorf("close duckdb: %w", err)
	}
	return nil
}
