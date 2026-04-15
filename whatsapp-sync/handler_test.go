package main

import (
	"path/filepath"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

func TestHandleMessageKeepsOlderGroupMessageWhenStoreHasNewerChat(t *testing.T) {
	store := newTestDuckStore(t)

	newer := time.Date(2026, time.March, 27, 9, 18, 59, 0, time.UTC)
	if err := store.InsertMessages([]MessageRecord{{
		ID:        "dm-newer",
		ChatJID:   "15550001111@s.whatsapp.net",
		SenderJID: "15550001111@s.whatsapp.net",
		IsFromMe:  false,
		IsGroup:   false,
		Timestamp: newer,
		MsgType:   "text",
	}}); err != nil {
		t.Fatalf("seed newer message: %v", err)
	}

	handler := NewEventHandler(nil, store)
	olderGroupMsg := newTestMessageEvent(
		t,
		"group-older",
		"120363123456789012@g.us",
		"15551230000@s.whatsapp.net",
		newer.Add(-45*time.Minute),
		true,
	)

	handler.handleMessage(olderGroupMsg)

	var count int
	if err := store.db.QueryRow("SELECT COUNT(*) FROM messages").Scan(&count); err != nil {
		t.Fatalf("count messages: %v", err)
	}
	if count != 2 {
		t.Fatalf("expected 2 messages after inserting older group message, got %d", count)
	}

	var isGroup bool
	var ts time.Time
	if err := store.db.QueryRow(
		"SELECT is_group, timestamp FROM messages WHERE id = ? AND chat_jid = ?",
		olderGroupMsg.Info.ID,
		olderGroupMsg.Info.Chat.String(),
	).Scan(&isGroup, &ts); err != nil {
		t.Fatalf("query inserted group message: %v", err)
	}
	if !isGroup {
		t.Fatalf("expected inserted message to be marked as a group message")
	}
	if !ts.Equal(olderGroupMsg.Info.Timestamp) {
		t.Fatalf("expected timestamp %s, got %s", olderGroupMsg.Info.Timestamp, ts)
	}
}

func TestHandleMessageReliesOnPrimaryKeyDeduplication(t *testing.T) {
	store := newTestDuckStore(t)
	handler := NewEventHandler(nil, store)
	msg := newTestMessageEvent(
		t,
		"dup-1",
		"120363123456789012@g.us",
		"15551230000@s.whatsapp.net",
		time.Date(2026, time.March, 27, 8, 0, 0, 0, time.UTC),
		true,
	)

	handler.handleMessage(msg)
	handler.handleMessage(msg)

	var count int
	if err := store.db.QueryRow(
		"SELECT COUNT(*) FROM messages WHERE id = ? AND chat_jid = ?",
		msg.Info.ID,
		msg.Info.Chat.String(),
	).Scan(&count); err != nil {
		t.Fatalf("count deduplicated messages: %v", err)
	}
	if count != 1 {
		t.Fatalf("expected duplicate live events to collapse to one stored row, got %d", count)
	}
}

func TestHandleHistorySyncWaitsForTerminalProgressBeforeQuickCompletion(t *testing.T) {
	store := newTestDuckStore(t)
	handler := NewEventHandler(nil, store)
	handler.historyIdleTimeout = 40 * time.Millisecond
	handler.terminalSettleTimeout = 15 * time.Millisecond

	handler.handleHistorySync(&events.HistorySync{
		Data: &waHistorySync.HistorySync{
			SyncType:   waHistorySync.HistorySync_INITIAL_BOOTSTRAP.Enum(),
			ChunkOrder: proto.Uint32(1),
			Progress:   proto.Uint32(42),
		},
	})

	select {
	case <-handler.Done():
		t.Fatalf("sync completed before terminal progress")
	case <-time.After(10 * time.Millisecond):
	}

	handler.handleHistorySync(&events.HistorySync{
		Data: &waHistorySync.HistorySync{
			SyncType:   waHistorySync.HistorySync_INITIAL_BOOTSTRAP.Enum(),
			ChunkOrder: proto.Uint32(2),
			Progress:   proto.Uint32(100),
		},
	})

	select {
	case <-handler.Done():
	case <-time.After(200 * time.Millisecond):
		t.Fatalf("sync did not complete after terminal progress")
	}
}

func newTestDuckStore(t *testing.T) *DuckStore {
	t.Helper()

	path := filepath.Join(t.TempDir(), "messages.duckdb")
	store, err := NewDuckStore(path)
	if err != nil {
		t.Fatalf("create test duck store: %v", err)
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Fatalf("close test duck store: %v", err)
		}
	})
	return store
}

func newTestMessageEvent(t *testing.T, id, chatRaw, senderRaw string, ts time.Time, isGroup bool) *events.Message {
	t.Helper()

	chat, err := types.ParseJID(chatRaw)
	if err != nil {
		t.Fatalf("parse chat JID %q: %v", chatRaw, err)
	}
	sender, err := types.ParseJID(senderRaw)
	if err != nil {
		t.Fatalf("parse sender JID %q: %v", senderRaw, err)
	}

	return &events.Message{
		Info: types.MessageInfo{
			MessageSource: types.MessageSource{
				Chat:     chat,
				Sender:   sender,
				IsFromMe: false,
				IsGroup:  isGroup,
			},
			ID:        types.MessageID(id),
			PushName:  "Test Sender",
			Timestamp: ts,
		},
	}
}
