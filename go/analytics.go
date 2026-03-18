package main

import (
	"fmt"
	"strings"
)

// availableReports lists all named reports with their descriptions.
var availableReports = []struct {
	name        string
	description string
}{
	{"summary", "Overall message statistics"},
	{"unanswered", "1:1 conversations where someone messaged you but you haven't replied in 48+ hours"},
	{"stale", "Contacts you used to message regularly (5+ messages) but haven't contacted in 30+ days"},
	{"response-time", "Average and median time you take to reply, per contact (1:1 chats only)"},
	{"top-contacts", "Top 20 conversations by total message count"},
	{"heatmap", "Message activity broken down by day-of-week and hour"},
	{"search", "Keyword search across message text (requires a search term argument)"},
	{"all", "Runs summary, unanswered, stale, and top-contacts in sequence"},
}

// PrintAvailableReports prints all available report names with their descriptions.
func PrintAvailableReports() {
	fmt.Println("Available reports:")
	fmt.Println()
	for _, r := range availableReports {
		fmt.Printf("  %-16s %s\n", r.name, r.description)
	}
	fmt.Println()
	fmt.Println("Usage: -report <name> [args...]")
	fmt.Println("Example: -report search \"hello world\"")
}

// RunAnalytics dispatches a named report against the given DuckStore.
func RunAnalytics(store *DuckStore, report string, args ...string) error {
	switch report {
	case "summary":
		return runSummary(store)
	case "unanswered":
		return runUnanswered(store)
	case "stale":
		return runStale(store)
	case "response-time":
		return runResponseTime(store)
	case "top-contacts":
		return runTopContacts(store)
	case "heatmap":
		return runHeatmap(store)
	case "search":
		if len(args) == 0 || strings.TrimSpace(args[0]) == "" {
			return fmt.Errorf("search report requires a search term argument")
		}
		return runSearch(store, args[0])
	case "all":
		return runAll(store)
	default:
		fmt.Printf("Unknown report: %q\n\n", report)
		PrintAvailableReports()
		return fmt.Errorf("unknown report %q", report)
	}
}

func runSummary(store *DuckStore) error {
	fmt.Println("=== Summary ===")
	err := store.Query(`
SELECT
    COUNT(*) as total_messages,
    COUNT(DISTINCT chat_jid) as total_chats,
    COUNT(DISTINCT sender_jid) as unique_senders,
    MIN(timestamp) as earliest_message,
    MAX(timestamp) as latest_message,
    SUM(CASE WHEN is_from_me THEN 1 ELSE 0 END) as sent_by_me,
    SUM(CASE WHEN NOT is_from_me THEN 1 ELSE 0 END) as received
FROM messages`)
	fmt.Println()
	return err
}

func runUnanswered(store *DuckStore) error {
	fmt.Println("=== Unanswered Conversations ===")
	err := store.Query(`
WITH chat_stats AS (
    SELECT chat_jid,
           MAX(timestamp) as last_msg_time,
           MAX(timestamp) FILTER (WHERE is_from_me) as last_sent_time,
           MAX(timestamp) FILTER (WHERE NOT is_from_me) as last_received_time,
           LAST(text_content ORDER BY timestamp) as last_text,
           LAST(push_name ORDER BY timestamp) as last_push_name,
           LAST(text_content ORDER BY timestamp) FILTER (WHERE NOT is_from_me) as their_last_text,
           COUNT(*) as total_msgs,
           COUNT(*) FILTER (WHERE is_from_me) as my_msgs
    FROM messages
    WHERE NOT is_group
    GROUP BY chat_jid
)
SELECT last_push_name as contact,
       chat_jid,
       last_received_time as their_last_msg,
       EXTRACT(EPOCH FROM (NOW() - last_received_time))/3600 as hours_ago,
       LEFT(their_last_text, 80) as their_message,
       total_msgs
FROM chat_stats
WHERE last_received_time > COALESCE(last_sent_time, '1970-01-01'::TIMESTAMPTZ)
  AND last_received_time < NOW() - INTERVAL '48 hours'
  AND total_msgs >= 2
  AND LENGTH(TRIM(COALESCE(their_last_text, ''))) > 2
  AND NOT REGEXP_MATCHES(TRIM(their_last_text), '^[\x{1F44D}\x{1F44C}\x{1F64F}\x{2764}\x{FE0F}\x{1F602}\x{1F60A}\x{1F525}\x{2705}\x{1F4AF}\x{1F44B}\x{1F389}\x{1F38A}\x{1F609}\x{2B50}\x{1F499}\x{1F49A}\x{1F90D}\x{1F929}\x{1F913}\x{1F917}\s]+$')
  AND NOT REGEXP_MATCHES(LOWER(TRIM(REGEXP_REPLACE(their_last_text, '[\x{1F600}-\x{1F64F}\x{1F300}-\x{1F5FF}\x{1F680}-\x{1F6FF}\x{1F1E0}-\x{1F1FF}\x{2600}-\x{27BF}\x{FE00}-\x{FE0F}\x{1F900}-\x{1F9FF}]', '', 'g'))),
      '^(thanks?(\s+\w+)?|thank\s+you(\s+\w+)?|ok(ay)?|sure(\s+\w+)?|done|great|nice|cool|perfect|noted|will\s+do|sounds?\s+good|got\s+it|np|no\s+worries|no\s+probs?|haha|lol|yes|ya|yep|yup|nope|no|haan(\s+sure)?|let\s+me\s+see|acha|theek\s+hai|all\s+good|good|awesome|amazing|wonderful|fantastic)([.!,\s]+(thanks?(\s+\w+)*|thank\s+you(\s+\w+)*|got\s+it|noted|will\s+do|sure|ok|cheers|much\s+appreciated|really\s+appreciate\s+it))?[.!,\s]*$'
  )
ORDER BY last_received_time DESC
LIMIT 20`)
	fmt.Println()
	return err
}

func runStale(store *DuckStore) error {
	fmt.Println("=== Stale Contacts ===")
	err := store.Query(`
WITH contact_activity AS (
    SELECT chat_jid,
           LAST(push_name ORDER BY timestamp) as contact_name,
           COUNT(*) as total_messages,
           MAX(timestamp) as last_message,
           SUM(CASE WHEN is_from_me THEN 1 ELSE 0 END) as my_messages
    FROM messages
    WHERE NOT is_group
    GROUP BY chat_jid
    HAVING COUNT(*) >= 5
)
SELECT contact_name,
       chat_jid,
       total_messages,
       my_messages,
       last_message,
       EXTRACT(DAY FROM (NOW() - last_message)) as days_inactive
FROM contact_activity
WHERE last_message < NOW() - INTERVAL '30 days'
ORDER BY total_messages DESC
LIMIT 20`)
	fmt.Println()
	return err
}

func runResponseTime(store *DuckStore) error {
	fmt.Println("=== Response Time Analysis ===")
	err := store.Query(`
WITH responses AS (
    SELECT m1.chat_jid,
           m1.timestamp as received_at,
           MIN(m2.timestamp) as replied_at
    FROM messages m1
    JOIN messages m2 ON m1.chat_jid = m2.chat_jid
        AND m2.is_from_me
        AND m2.timestamp > m1.timestamp
        AND m2.timestamp < m1.timestamp + INTERVAL '24 hours'
    WHERE NOT m1.is_from_me AND NOT m1.is_group
    GROUP BY m1.chat_jid, m1.timestamp
)
SELECT r.chat_jid,
       COALESCE(c.push_name, c.full_name, r.chat_jid) as contact,
       COUNT(*) as replies,
       ROUND(AVG(EXTRACT(EPOCH FROM (replied_at - received_at))/60), 1) as avg_reply_minutes,
       ROUND(MEDIAN(EXTRACT(EPOCH FROM (replied_at - received_at))/60), 1) as median_reply_minutes
FROM responses r
LEFT JOIN contacts c ON r.chat_jid = c.jid
GROUP BY r.chat_jid, c.push_name, c.full_name
ORDER BY avg_reply_minutes ASC
LIMIT 20`)
	fmt.Println()
	return err
}

func runTopContacts(store *DuckStore) error {
	fmt.Println("=== Top Contacts ===")
	err := store.Query(`
SELECT chat_jid,
       LAST(push_name ORDER BY timestamp) as contact_name,
       is_group,
       COUNT(*) as total_messages,
       SUM(CASE WHEN is_from_me THEN 1 ELSE 0 END) as sent,
       SUM(CASE WHEN NOT is_from_me THEN 1 ELSE 0 END) as received,
       MIN(timestamp) as first_message,
       MAX(timestamp) as last_message
FROM messages
GROUP BY chat_jid, is_group
ORDER BY total_messages DESC
LIMIT 20`)
	fmt.Println()
	return err
}

func runHeatmap(store *DuckStore) error {
	fmt.Println("=== Activity Heatmap (Day x Hour) ===")
	err := store.Query(`
SELECT
    CASE sent_dow
        WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue'
        WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri' WHEN 6 THEN 'Sat'
    END as day,
    sent_hour as hour,
    COUNT(*) as messages
FROM messages
GROUP BY sent_dow, sent_hour
ORDER BY sent_dow, sent_hour`)
	fmt.Println()
	return err
}

func runSearch(store *DuckStore, term string) error {
	fmt.Printf("=== Search: %q ===\n", term)
	// Escape single quotes to prevent SQL injection / syntax errors.
	escaped := strings.ReplaceAll(term, "'", "''")
	sql := fmt.Sprintf(`
SELECT timestamp, push_name, chat_jid, LEFT(text_content, 120) as message
FROM messages
WHERE text_content ILIKE '%%%s%%'
ORDER BY timestamp DESC
LIMIT 30`, escaped)
	err := store.Query(sql)
	fmt.Println()
	return err
}

func runAll(store *DuckStore) error {
	if err := runSummary(store); err != nil {
		return err
	}
	if err := runUnanswered(store); err != nil {
		return err
	}
	if err := runStale(store); err != nil {
		return err
	}
	return runTopContacts(store)
}
