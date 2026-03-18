package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
)

// DownloadOpts controls which messages are downloaded and where files are written.
type DownloadOpts struct {
	After     time.Time
	Before    time.Time
	ChatJID   string
	Types     []string // "image", "video", "audio", "document"
	OutputDir string
}

// DownloadAttachments queries the store for undownloaded media messages matching
// opts, downloads each one via the WhatsApp client, and persists the files to
// OutputDir. Individual download failures are logged and skipped so the rest of
// the batch continues.
func DownloadAttachments(ctx context.Context, client *whatsmeow.Client, store *DuckStore, opts DownloadOpts) error {
	messages, err := store.GetMessagesForDownload(opts.After, opts.Before, opts.ChatJID, opts.Types)
	if err != nil {
		return fmt.Errorf("query messages for download: %w", err)
	}

	if len(messages) == 0 {
		log.Println("media: No media to download")
		return nil
	}

	total := len(messages)
	log.Printf("media: Found %d messages with media to download", total)

	downloaded := 0
	failed := 0
	skipped := 0

	for i, msg := range messages {
		idx := i + 1

		// Unmarshal the raw protobuf into a full Message.
		var waMsg waE2E.Message
		if err := proto.Unmarshal(msg.RawProto, &waMsg); err != nil {
			log.Printf("media: [%d/%d] WARNING: failed to unmarshal proto for %s/%s: %v — skipping",
				idx, total, msg.ChatJID, msg.ID, err)
			skipped++
			continue
		}

		// Extract the concrete downloadable sub-message.
		downloadable, ext := getDownloadable(&waMsg)
		if downloadable == nil {
			log.Printf("media: [%d/%d] WARNING: unknown media type %q for %s/%s — skipping",
				idx, total, msg.MsgType, msg.ChatJID, msg.ID)
			skipped++
			continue
		}

		// Download the media bytes.
		data, err := client.Download(ctx, downloadable)
		if err != nil {
			log.Printf("media: [%d/%d] WARNING: download failed for %s/%s (%s): %v — skipping",
				idx, total, msg.ChatJID, msg.ID, msg.MsgType, err)
			failed++
			continue
		}

		// Build output path: {OutputDir}/{sanitizedChatJID}/{YYYY-MM-DD}_{id}.{ext}
		dateStr := msg.Timestamp.UTC().Format("2006-01-02")
		filename := fmt.Sprintf("%s_%s.%s", dateStr, msg.ID, ext)
		chatDir := filepath.Join(opts.OutputDir, sanitizeJID(msg.ChatJID))
		outputPath := filepath.Join(chatDir, filename)

		if err := os.MkdirAll(chatDir, 0o755); err != nil {
			log.Printf("media: [%d/%d] WARNING: failed to create directory %q for %s/%s: %v — skipping",
				idx, total, chatDir, msg.ChatJID, msg.ID, err)
			failed++
			continue
		}

		if err := os.WriteFile(outputPath, data, 0o644); err != nil {
			log.Printf("media: [%d/%d] WARNING: failed to write file %q for %s/%s: %v — skipping",
				idx, total, outputPath, msg.ChatJID, msg.ID, err)
			failed++
			continue
		}

		// Record the download in the store.
		if err := store.MarkMediaDownloaded(msg.ID, msg.ChatJID, outputPath); err != nil {
			log.Printf("media: [%d/%d] WARNING: failed to mark media downloaded for %s/%s: %v",
				idx, total, msg.ChatJID, msg.ID, err)
			// Not fatal — the file is on disk, so log and continue.
		}

		log.Printf("media: [%d/%d] Downloaded %s: %s (%s)",
			idx, total, msg.MsgType, filename, humanSize(len(data)))
		downloaded++
	}

	log.Printf("media: Downloaded %d files (%d failed, %d skipped)", downloaded, failed, skipped)
	return nil
}

// getDownloadable returns the downloadable sub-message contained in msg along
// with an appropriate file extension. Both return values are nil/"" if the
// message does not contain a known downloadable media type.
func getDownloadable(msg *waE2E.Message) (whatsmeow.DownloadableMessage, string) {
	if img := msg.GetImageMessage(); img != nil {
		return img, extFromMime(img.GetMimetype(), "jpg")
	}
	if vid := msg.GetVideoMessage(); vid != nil {
		return vid, extFromMime(vid.GetMimetype(), "mp4")
	}
	if aud := msg.GetAudioMessage(); aud != nil {
		return aud, extFromMime(aud.GetMimetype(), "ogg")
	}
	if doc := msg.GetDocumentMessage(); doc != nil {
		ext := extFromMime(doc.GetMimetype(), "bin")
		// Prefer the original filename extension when one is available.
		if fname := doc.GetFileName(); fname != "" {
			if i := strings.LastIndex(fname, "."); i >= 0 {
				ext = fname[i+1:]
			}
		}
		return doc, ext
	}
	return nil, ""
}

// extFromMime maps a MIME type string to a file extension. If the MIME type is
// not recognised, fallback is returned.
func extFromMime(mime, fallback string) string {
	switch mime {
	case "image/jpeg":
		return "jpg"
	case "image/png":
		return "png"
	case "image/webp":
		return "webp"
	case "video/mp4":
		return "mp4"
	case "audio/ogg":
		return "ogg"
	case "audio/mpeg":
		return "mp3"
	case "application/pdf":
		return "pdf"
	default:
		return fallback
	}
}

// sanitizeJID replaces characters that are unsafe in directory names with
// underscores so that JIDs (e.g. "123456789@s.whatsapp.net") can be used as
// directory components.
func sanitizeJID(jid string) string {
	return strings.NewReplacer("@", "_", ":", "_").Replace(jid)
}

// humanSize formats a byte count as a human-readable string using KB, MB, or
// GB suffixes. Values below 1 KB are reported in bytes.
func humanSize(bytes int) string {
	const (
		kb = 1024
		mb = 1024 * kb
		gb = 1024 * mb
	)
	switch {
	case bytes >= gb:
		return fmt.Sprintf("%.2f GB", float64(bytes)/float64(gb))
	case bytes >= mb:
		return fmt.Sprintf("%.2f MB", float64(bytes)/float64(mb))
	case bytes >= kb:
		return fmt.Sprintf("%.2f KB", float64(bytes)/float64(kb))
	default:
		return fmt.Sprintf("%d B", bytes)
	}
}
