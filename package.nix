{ lib, stdenvNoCC, glib, makeWrapper, python3 }:
stdenvNoCC.mkDerivation {
  pname = "hapticspp";
  version = "unstable-2026-10-02";
  src = lib.cleanSourceWith {
    src = ./.;
    filter = path: type:
      let relative = lib.removePrefix (toString ./. + "/") path;
      in path == toString ./. || builtins.elem (builtins.head (lib.splitString "/" relative))
        [ "daemon" "hidpp" "extension" "nix" ] && !(lib.hasInfix "__pycache__" path);
  };
  nativeBuildInputs = [ glib makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/share/hapticspp" "$out/share/gnome-shell/extensions/hapticspp@doromiert" "$out/lib/udev/rules.d"
    cp -r daemon hidpp "$out/share/hapticspp/"
    cp -r extension/. "$out/share/gnome-shell/extensions/hapticspp@doromiert/"
    glib-compile-schemas --strict "$out/share/gnome-shell/extensions/hapticspp@doromiert/schemas"
    cp nix/99-hidpp-haptics.rules "$out/lib/udev/rules.d/70-hidpp-haptics.rules"
    makeWrapper ${python3}/bin/python3 "$out/bin/hapticspp-daemon" \
      --add-flags "$out/share/hapticspp/daemon/hapticsd.py"
    runHook postInstall
  '';
  meta = { description = "GNOME cursor haptics for Logitech HID++ mice"; platforms = lib.platforms.linux; mainProgram = "hapticspp-daemon"; license = lib.licenses.unfree; };
  passthru.extensionUuid = "hapticspp@doromiert";
}
