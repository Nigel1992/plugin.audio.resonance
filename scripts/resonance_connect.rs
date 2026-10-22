//! Framed PCM/events for Kodi. Enabled only by RESONANCE_CONNECT_BRIDGE=1.
use std::{io::{self, BufRead, Write}, sync::Mutex, time::{Duration, Instant}};
use librespot::playback::{audio_backend::{Sink, SinkResult}, config::AudioFormat,
    convert::Converter, decoder::AudioPacket, player::{Player, PlayerEvent}};
use serde_json::{json, Value};

static OUTPUT: Mutex<()> = Mutex::new(());

fn frame(kind: u8, payload: &[u8]) {
    let _guard = OUTPUT.lock().unwrap();
    let mut out = io::stdout().lock();
    let mut header = [0u8; 9];
    header[..4].copy_from_slice(b"RSC1");
    header[4] = kind;
    header[5..].copy_from_slice(&(payload.len() as u32).to_le_bytes());
    if out.write_all(&header).and_then(|_| out.write_all(payload)).and_then(|_| out.flush()).is_err() {
        std::process::exit(0);
    }
}

struct KodiSink { deadline: Instant, report: Instant, bytes: usize, blocked: Duration, max_gap: Duration, last: Instant }
pub fn sink(_format: AudioFormat) -> Box<dyn Sink> {
    let now = Instant::now();
    Box::new(KodiSink { deadline: now, report: now, bytes: 0, blocked: Duration::ZERO, max_gap: Duration::ZERO, last: now })
}
impl Sink for KodiSink {
    fn start(&mut self) -> SinkResult<()> { self.deadline = Instant::now(); Ok(()) }
    fn write(&mut self, packet: AudioPacket, converter: &mut Converter) -> SinkResult<()> {
        if let AudioPacket::Samples(samples) = packet {
            let pcm = converter.f64_to_s16(&samples);
            let bytes: Vec<u8> = pcm.iter().flat_map(|v| v.to_le_bytes()).collect();
            let before = Instant::now();
            self.max_gap = self.max_gap.max(before.duration_since(self.last));
            frame(b'A', &bytes);
            self.blocked += before.elapsed();
            self.bytes += bytes.len();
            if self.report.elapsed() >= Duration::from_secs(10) {
                eprintln!("Resonance sink: bytes={} elapsed_ms={} pipe_ms={} gap_ms={}", self.bytes, self.report.elapsed().as_millis(), self.blocked.as_millis(), self.max_gap.as_millis());
                self.report = Instant::now(); self.bytes = 0; self.blocked = Duration::ZERO; self.max_gap = Duration::ZERO;
            }
            // A pipe can consume audio faster than real time. Pace the receiver
            // so Spotify's position/queue reflect what Kodi can actually play.
            self.deadline += Duration::from_secs_f64(bytes.len() as f64 / 176400.0);
            let now = Instant::now();
            // Kodi probes and prebuffers before starting its audio sink.
            // Keep a small reserve ahead of wall time rather than forcing
            // every read to wait for the next real-time decoder packet.
            let buffered_now = now + Duration::from_secs(3);
            if self.deadline > buffered_now { std::thread::sleep(self.deadline - buffered_now); }
            else if now.duration_since(self.deadline) > Duration::from_millis(500) { self.deadline = now; }
            self.last = Instant::now();
        }
        Ok(())
    }
}

pub fn events(player: &Player) {
    frame(b'E', b"{\"event\":\"ready\",\"protocol\":1}");
    let mut events = player.get_player_event_channel();
    tokio::spawn(async move {
        while let Some(event) = events.recv().await {
            let value = match event {
                PlayerEvent::Loading { track_id, position_ms, .. } => json!({"event":"loading", "uri":track_id.to_uri(), "position_ms":position_ms}),
                PlayerEvent::Playing { track_id, position_ms, .. } => json!({"event":"playing", "uri":track_id.to_uri(), "position_ms":position_ms}),
                PlayerEvent::Paused { position_ms, .. } => json!({"event":"paused", "position_ms":position_ms}),
                PlayerEvent::Seeked { position_ms, .. } => json!({"event":"seeked", "position_ms":position_ms}),
                PlayerEvent::Stopped { .. } => json!({"event":"stopped"}),
                PlayerEvent::Unavailable { .. } => json!({"event":"unavailable"}),
                PlayerEvent::TrackChanged { audio_item } => json!({"event":"track", "uri":audio_item.uri,
                    "name":audio_item.name, "duration_ms":audio_item.duration_ms,
                    "cover":audio_item.covers.first().map(|c| c.url.clone()).unwrap_or_default()}),
                _ => continue,
            };
            frame(b'E', &serde_json::to_vec(&value).unwrap());
        }
    });
}

pub fn commands() -> tokio::sync::mpsc::UnboundedReceiver<Value> {
    let (sender, receiver) = tokio::sync::mpsc::unbounded_channel();
    std::thread::spawn(move || {
        for line in io::stdin().lock().lines().map_while(Result::ok) {
            if let Ok(value) = serde_json::from_str(&line) {
                if sender.send(value).is_err() { break; }
            }
        }
    });
    receiver
}
