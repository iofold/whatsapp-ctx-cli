package main

import (
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
)

// EventHandler receives whatsmeow events, extracts message data, and persists
// it via DuckStore.
type EventHandler struct {
	client    *whatsmeow.Client
	store     *DuckStore
	msgCount  atomic.Int64  // total messages processed
	syncCount atomic.Int64  // history sync events received
	done      chan struct{} // closed when history sync is considered complete
	doneOnce  sync.Once     // ensures done is closed only once
	idleTimer *time.Timer   // reset on each HistorySync event

	initialIdleTimeout    time.Duration
	historyIdleTimeout    time.Duration
	terminalSettleTimeout time.Duration
}

const (
	defaultInitialIdleTimeout    = 30 * time.Second
	defaultHistoryIdleTimeout    = 10 * time.Minute
	defaultTerminalSettleTimeout = 20 * time.Second
)

// NewEventHandler constructs an EventHandler wired to the given client and
// store. Register its HandleEvent method with the whatsmeow client:
//
//	client.AddEventHandler(h.HandleEvent)
func NewEventHandler(client *whatsmeow.Client, store *DuckStore) *EventHandler {
	return &EventHandler{
		client:                client,
		store:                 store,
		done:                  make(chan struct{}),
		initialIdleTimeout:    defaultInitialIdleTimeout,
		historyIdleTimeout:    defaultHistoryIdleTimeout,
		terminalSettleTimeout: defaultTerminalSettleTimeout,
	}
}

// HandleEvent is the top-level whatsmeow event dispatcher. Register it with
// client.AddEventHandler.
func (h *EventHandler) HandleEvent(evt interface{}) {
	switch v := evt.(type) {
	case *events.HistorySync:
		h.handleHistorySync(v)
	case *events.Message:
		h.handleMessage(v)
	case *events.Connected:
		log.Println("Connected to WhatsApp")
		// Start idle timer on connect — if no HistorySync arrives soon after connect,
		// consider sync complete (e.g., incremental mode with no pending history).
		if h.idleTimer == nil {
			h.idleTimer = time.AfterFunc(h.initialIdleTimeout, func() {
				log.Println("sync: no history sync events received, sync considered complete")
				h.signalDone()
			})
		}
	case *events.LoggedOut:
		log.Println("Logged out from WhatsApp")
	case *events.PushNameSetting:
		log.Printf("Push name setting: %q", v.Action.GetName())
	}
}

// handleHistorySync processes a HistorySync event by iterating all
// conversations, parsing each web message, and batch-inserting the results.
func (h *EventHandler) handleHistorySync(evt *events.HistorySync) {
	syncType := evt.Data.GetSyncType().String()
	conversations := evt.Data.GetConversations()
	progress := evt.Data.GetProgress()
	chunk := evt.Data.GetChunkOrder()
	log.Printf(
		"History sync: type=%s chunk=%d progress=%d conversations=%d",
		syncType,
		chunk,
		progress,
		len(conversations),
	)

	var records []MessageRecord

	for _, conv := range conversations {
		rawJID := conv.GetID()
		if rawJID == "" {
			continue
		}

		chatJID, err := types.ParseJID(rawJID)
		if err != nil {
			log.Printf("History sync: failed to parse JID %q: %v", rawJID, err)
			continue
		}

		for _, histMsg := range conv.GetMessages() {
			webMsg := histMsg.GetMessage()
			if webMsg == nil {
				continue
			}

			parsed, err := h.client.ParseWebMessage(chatJID, webMsg)
			if err != nil {
				log.Printf("History sync: failed to parse message in %q: %v", rawJID, err)
				continue
			}

			records = append(records, extractMessage(parsed))
		}
	}

	if len(records) > 0 {
		if err := h.store.InsertMessages(records); err != nil {
			log.Printf("History sync: failed to insert messages: %v", err)
		}
	}

	h.msgCount.Add(int64(len(records)))
	h.syncCount.Add(1)

	log.Printf("History sync: processed %d messages from %d conversations", len(records), len(conversations))

	// Reset idle timer.
	// While history is still in flight, keep a long idle window to avoid
	// disconnecting between sparse chunks. Once progress reaches 100, use a short
	// settle timer so any trailing batches can still arrive before we finish.
	if h.idleTimer != nil {
		h.idleTimer.Stop()
	}
	if progress >= 100 {
		h.idleTimer = time.AfterFunc(h.terminalSettleTimeout, func() {
			log.Printf(
				"sync: history sync reached 100%% and stayed idle for %s, sync considered complete",
				h.terminalSettleTimeout,
			)
			h.signalDone()
		})
	} else {
		h.idleTimer = time.AfterFunc(h.historyIdleTimeout, func() {
			log.Printf(
				"sync: no new history events for %s after progress=%d, sync considered complete",
				h.historyIdleTimeout,
				progress,
			)
			h.signalDone()
		})
	}
}

// handleMessage processes a single live Message event.
// We intentionally skip watermark filtering here — INSERT OR IGNORE on the
// (id, chat_jid) primary key already handles deduplication. Filtering by a
// global watermark caused messages in less-active chats (e.g. groups) to be
// silently dropped when other chats had newer timestamps.
func (h *EventHandler) handleMessage(evt *events.Message) {
	record := extractMessage(evt)

	if err := h.store.InsertMessages([]MessageRecord{record}); err != nil {
		log.Printf("Message: failed to insert message %q: %v", record.ID, err)
	}

	h.msgCount.Add(1)
}

// extractMessage converts a whatsmeow Message event into a MessageRecord.
func extractMessage(msg *events.Message) MessageRecord {
	msgType, textContent, mediaType := extractContent(msg.Message)

	var rawProto []byte
	if msg.Message != nil {
		rawProto, _ = proto.Marshal(msg.Message)
	}

	record := MessageRecord{
		ID:          msg.Info.ID,
		ChatJID:     msg.Info.Chat.String(),
		SenderJID:   msg.Info.Sender.String(),
		IsFromMe:    msg.Info.IsFromMe,
		IsGroup:     msg.Info.IsGroup,
		Timestamp:   msg.Info.Timestamp,
		PushName:    msg.Info.PushName,
		MsgType:     msgType,
		TextContent: textContent,
		MediaType:   mediaType,
		RawProto:    rawProto,
	}

	return record
}

// extractContent inspects a waE2E.Message and returns the logical message
// type, any text/caption content, and the MIME type for media messages.
func extractContent(msg *waE2E.Message) (msgType, textContent, mediaType string) {
	if msg == nil {
		return "other", "", ""
	}

	if text := msg.GetConversation(); text != "" {
		return "text", text, ""
	}

	if ext := msg.GetExtendedTextMessage(); ext != nil {
		return "text", ext.GetText(), ""
	}

	if img := msg.GetImageMessage(); img != nil {
		return "image", img.GetCaption(), img.GetMimetype()
	}

	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", vid.GetCaption(), vid.GetMimetype()
	}

	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "", aud.GetMimetype()
	}

	if doc := msg.GetDocumentMessage(); doc != nil {
		return "document", doc.GetFileName(), doc.GetMimetype()
	}

	if sticker := msg.GetStickerMessage(); sticker != nil {
		return "sticker", "", sticker.GetMimetype()
	}

	if loc := msg.GetLocationMessage(); loc != nil {
		content := fmt.Sprintf("%f,%f", loc.GetDegreesLatitude(), loc.GetDegreesLongitude())
		return "location", content, ""
	}

	if contact := msg.GetContactMessage(); contact != nil {
		return "contact", contact.GetDisplayName(), ""
	}

	if reaction := msg.GetReactionMessage(); reaction != nil {
		return "reaction", reaction.GetText(), ""
	}

	return "other", "", ""
}

// signalDone closes the done channel exactly once, marking sync as complete.
func (h *EventHandler) signalDone() {
	h.doneOnce.Do(func() {
		close(h.done)
	})
}

// WaitForSync blocks until history sync is considered complete or the timeout
// elapses.
func (h *EventHandler) WaitForSync(timeout time.Duration) error {
	select {
	case <-h.done:
		return nil
	case <-time.After(timeout):
		return fmt.Errorf("sync timed out after %v", timeout)
	}
}

// Done returns a channel that is closed when history sync is considered
// complete.
func (h *EventHandler) Done() <-chan struct{} {
	return h.done
}

// Stats returns the current message and history-sync counters.
func (h *EventHandler) Stats() (messages int64, syncs int64) {
	return h.msgCount.Load(), h.syncCount.Load()
}
