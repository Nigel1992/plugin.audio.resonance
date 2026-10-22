"""Extend the pinned Spotty source with the opt-in Kodi Connect pipe."""
from pathlib import Path
import shutil
import sys

repo = Path(sys.argv[1])
main = Path("src/main.rs")
source = main.read_text()
if "mod resonance_connect;" not in source:
    source = "mod resonance_connect;\n" + source
    old = "    let device = setup.device.clone();\n    let player = Player::new"
    new = ('    let device = setup.device.clone();\n'
           '    let kodi_bridge = env::var("RESONANCE_CONNECT_BRIDGE").as_deref() == Ok("1");\n'
           '    let player = Player::new')
    assert old in source
    source = source.replace(old, new, 1)
    source = source.replace("        (backend)(device, format)\n", "        if kodi_bridge { resonance_connect::sink(format) } else { (backend)(device, format) }\n", 1)
    marker = '    #[cfg(not(feature = "spotty"))]\n    if let Some(player_event_program)'
    assert marker in source
    source = source.replace(marker, '    let mut kodi_commands = resonance_connect::commands();\n'
                            '    if kodi_bridge { resonance_connect::events(&player); }\n\n' + marker, 1)
    marker = "        tokio::select! {\n            credentials = async {"
    assert marker in source
    source = source.replace(marker, '''        tokio::select! {
            Some(command) = kodi_commands.recv(), if kodi_bridge => {
                if let Some(remote) = spirc.as_ref() {
                    let result = match command["command"].as_str().unwrap_or("") {
                        "pause" => remote.pause(),
                        "play" => remote.play(),
                        "next" => remote.next(),
                        "previous" => remote.prev(),
                        "disconnect" => remote.disconnect(true),
                        "seek" => remote.set_position_ms(command["position_ms"].as_u64().unwrap_or(0) as u32),
                        _ => Ok(()),
                    };
                    if let Err(error) = result { warn!("Kodi Connect command failed: {error}"); }
                }
            },
            credentials = async {''', 1)
    main.write_text(source)
shutil.copyfile(repo / "scripts/resonance_connect.rs", "src/resonance_connect.rs")
p = Path("src/spotty.rs")
s = p.read_text()
if '"connect-bridge"' not in s:
    s = s.replace('"autoplay": true,', '"connect-bridge": true,\n        "autoplay": true,', 1)
    p.write_text(s)
