package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(1)
	}

	switch os.Args[1] {
	case "sync":
		runSyncCmd(os.Args[2:])
	case "analytics":
		runAnalyticsCmd(os.Args[2:])
	case "download":
		runDownloadCmd(os.Args[2:])
	case "help", "--help", "-h":
		printUsage()
	default:
		fmt.Fprintf(os.Stderr, "Unknown command: %s\n\n", os.Args[1])
		printUsage()
		os.Exit(1)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, `Usage: whatsapp-sync <command> [flags]

Commands:
  sync        Connect to WhatsApp and sync messages
  analytics   Run analytics reports on synced data
  download    Download media attachments

Run "whatsapp-sync <command> --help" for command-specific flags.
`)
}

// connectWhatsApp creates and connects a whatsmeow client. If showQR is true
// and no session exists yet, it runs the QR login flow; otherwise it connects
// directly with the existing session.
func connectWhatsApp(ctx context.Context, waDBPath string, showQR bool) (*whatsmeow.Client, error) {
	dbLog := waLog.Stdout("DB", "WARN", true)
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+waDBPath+"?_foreign_keys=on", dbLog)
	if err != nil {
		return nil, fmt.Errorf("failed to create sqlstore container: %w", err)
	}

	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to get first device: %w", err)
	}

	clientLog := waLog.Stdout("Client", "WARN", true)
	client := whatsmeow.NewClient(device, clientLog)

	if showQR && client.Store.ID == nil {
		// Not logged in — run QR login flow.
		qrChan, err := client.GetQRChannel(ctx)
		if err != nil {
			return nil, fmt.Errorf("failed to get QR channel: %w", err)
		}

		if err := client.Connect(); err != nil {
			return nil, fmt.Errorf("failed to connect: %w", err)
		}

		for evt := range qrChan {
			if evt.Event == "code" {
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			} else {
				log.Printf("QR event: %s", evt.Event)
			}
		}
	} else {
		// Already logged in (or QR not requested) — connect directly.
		if err := client.Connect(); err != nil {
			return nil, fmt.Errorf("failed to connect: %w", err)
		}
	}

	return client, nil
}

func runSyncCmd(args []string) {
	fs := flag.NewFlagSet("sync", flag.ExitOnError)
	live := fs.Bool("live", false, "keep running after sync completes")
	incremental := fs.Bool("incremental", false, "only sync new messages since last run")
	dbPath := fs.String("db", "messages.duckdb", "path to DuckDB database file")
	waDBPath := fs.String("wa-db", "whatsmeow.db", "path to whatsmeow SQLite session store")
	timeout := fs.Duration("timeout", 5*time.Minute, "max time to wait for history sync")
	if err := fs.Parse(args); err != nil {
		log.Fatalf("Failed to parse sync flags: %v", err)
	}

	// Configure full history sync BEFORE creating the store.
	store.DeviceProps.RequireFullSync = proto.Bool(true)
	store.DeviceProps.HistorySyncConfig = &waCompanionReg.DeviceProps_HistorySyncConfig{
		FullSyncDaysLimit:   proto.Uint32(1095),  // 3 years
		FullSyncSizeMbLimit: proto.Uint32(10240), // 10 GB
		StorageQuotaMb:      proto.Uint32(10240),
	}

	ctx := context.Background()

	duckStore, err := NewDuckStore(*dbPath)
	if err != nil {
		log.Fatalf("Failed to open DuckDB store: %v", err)
	}
	defer func() {
		if err := duckStore.Close(); err != nil {
			log.Printf("Failed to close DuckDB store: %v", err)
		}
	}()

	var watermark time.Time
	if *incremental {
		wm, err := duckStore.GetHighWatermark()
		if err != nil {
			log.Printf("Failed to get high watermark (starting from beginning): %v", err)
		} else {
			watermark = wm
			log.Printf("Incremental sync from watermark: %s", watermark.Format(time.RFC3339))
		}
	}

	client, err := connectWhatsApp(ctx, *waDBPath, true)
	if err != nil {
		log.Fatalf("Failed to connect to WhatsApp: %v", err)
	}
	defer client.Disconnect()

	handler := NewEventHandler(client, duckStore, *incremental, watermark)
	client.AddEventHandler(handler.HandleEvent)

	// Set up signal handler for graceful shutdown.
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	log.Println("Waiting for history sync to complete...")
	select {
	case <-handler.Done():
		log.Println("History sync complete")
	case <-time.After(*timeout):
		log.Printf("Timed out after %s waiting for history sync", *timeout)
	case <-quit:
		log.Println("Interrupted, shutting down...")
	}

	syncContacts(client, duckStore)

	messages, syncs := handler.Stats()
	log.Printf("Stats: %d messages processed, %d history sync events", messages, syncs)

	if *live {
		log.Println("Live mode: listening for new messages... Press Ctrl+C to exit")
		<-quit
	}
}

func runAnalyticsCmd(args []string) {
	fs := flag.NewFlagSet("analytics", flag.ExitOnError)
	report := fs.String("report", "all", "which analytics report to run")
	search := fs.String("search", "", "search term for the search report")
	dbPath := fs.String("db", "messages.duckdb", "path to DuckDB database file")
	if err := fs.Parse(args); err != nil {
		log.Fatalf("Failed to parse analytics flags: %v", err)
	}

	duckStore, err := NewDuckStore(*dbPath)
	if err != nil {
		log.Fatalf("Failed to open DuckDB store: %v", err)
	}
	defer func() {
		if err := duckStore.Close(); err != nil {
			log.Printf("Failed to close DuckDB store: %v", err)
		}
	}()

	if *report == "search" {
		if *search == "" {
			log.Fatalf("--search term is required when --report=search")
		}
		if err := RunAnalytics(duckStore, *report, *search); err != nil {
			log.Fatalf("Analytics failed: %v", err)
		}
	} else {
		if err := RunAnalytics(duckStore, *report); err != nil {
			log.Fatalf("Analytics failed: %v", err)
		}
	}
}

func runDownloadCmd(args []string) {
	fs := flag.NewFlagSet("download", flag.ExitOnError)
	after := fs.String("after", "", "only download media after this date (YYYY-MM-DD)")
	before := fs.String("before", "", "only download media before this date (YYYY-MM-DD)")
	chat := fs.String("chat", "", "filter by chat JID")
	types := fs.String("types", "image,video,audio,document", "comma-separated list of media types to download")
	output := fs.String("output", "./media", "output directory for downloaded files")
	dbPath := fs.String("db", "messages.duckdb", "path to DuckDB database file")
	waDBPath := fs.String("wa-db", "whatsmeow.db", "path to whatsmeow SQLite session store")
	if err := fs.Parse(args); err != nil {
		log.Fatalf("Failed to parse download flags: %v", err)
	}

	var afterTime, beforeTime time.Time
	if *after != "" {
		t, err := time.Parse("2006-01-02", *after)
		if err != nil {
			log.Fatalf("Invalid --after date %q (expected YYYY-MM-DD): %v", *after, err)
		}
		afterTime = t
	}
	if *before != "" {
		t, err := time.Parse("2006-01-02", *before)
		if err != nil {
			log.Fatalf("Invalid --before date %q (expected YYYY-MM-DD): %v", *before, err)
		}
		beforeTime = t
	}

	mediaTypes := strings.Split(*types, ",")
	for i, t := range mediaTypes {
		mediaTypes[i] = strings.TrimSpace(t)
	}

	ctx := context.Background()

	client, err := connectWhatsApp(ctx, *waDBPath, false)
	if err != nil {
		log.Fatalf("Failed to connect to WhatsApp: %v", err)
	}
	defer client.Disconnect()

	duckStore, err := NewDuckStore(*dbPath)
	if err != nil {
		log.Fatalf("Failed to open DuckDB store: %v", err)
	}
	defer func() {
		if err := duckStore.Close(); err != nil {
			log.Printf("Failed to close DuckDB store: %v", err)
		}
	}()

	opts := DownloadOpts{
		After:     afterTime,
		Before:    beforeTime,
		ChatJID:   *chat,
		Types:     mediaTypes,
		OutputDir: *output,
	}

	if err := DownloadAttachments(ctx, client, duckStore, opts); err != nil {
		log.Fatalf("Download failed: %v", err)
	}
}

func syncContacts(client *whatsmeow.Client, store *DuckStore) {
	ctx := context.Background()

	// Fetch latest contact app state before reading contacts.
	if err := client.FetchAppState(ctx, appstate.WAPatchCriticalUnblockLow, true, false); err != nil {
		log.Printf("Failed to fetch app state for contacts: %v", err)
	}

	contacts, err := client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		log.Printf("Failed to get contacts: %v", err)
	} else {
		for jid, info := range contacts {
			if err := store.UpsertContact(jid.String(), info.PushName, info.FullName, info.BusinessName, false, ""); err != nil {
				log.Printf("Failed to upsert contact %s: %v", jid, err)
			}
		}
		log.Printf("Synced %d contacts", len(contacts))
	}

	groups, err := client.GetJoinedGroups(ctx)
	if err != nil {
		log.Printf("Failed to get joined groups: %v", err)
	} else {
		for _, group := range groups {
			if err := store.UpsertContact(group.JID.String(), "", "", "", true, group.Name); err != nil {
				log.Printf("Failed to upsert group %s: %v", group.JID, err)
			}
		}
		log.Printf("Synced %d groups", len(groups))
	}
}
