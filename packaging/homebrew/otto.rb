class Otto < Formula
  desc "Voice and a screen for the Claude you already run (menu-bar assistant)"
  homepage "https://github.com/dockndevai/otto-assist"
  # Otto's Python deps (Whisper/torch/…) are too large and platform-specific to vendor as
  # Homebrew resources, and the app repo is private — so this is a HEAD-only formula that
  # clones over your SSH key. Install with:  brew install --HEAD dockndevai/otto/otto
  head "ssh://git@github.com/dockndevai/otto-assist.git", branch: "main"

  depends_on "ffmpeg"       # audio/video for the canvas + TTS pipeline
  depends_on :macos         # menu bar, `say`, launchd
  depends_on "portaudio"    # microphone capture (PyAudio builds against this)
  depends_on "python@3.12"

  def install
    # Vendor the source read-only; the venv is built in the user's data dir on first run.
    libexec.install Dir["*"]
    (bin/"otto").write <<~SH
      #!/bin/bash
      export OTTO_LIBEXEC="#{libexec}"
      export OTTO_BREW_PYTHON="#{formula_opt_bin("python@3.12")}/python3.12"
      export OTTO_LAUNCHD_LABEL="homebrew.mxcl.otto"
      exec "#{libexec}/scripts/otto-brew" "$@"
    SH
  end

  service do
    run [opt_bin/"otto", "menubar"]
    keep_alive true
    log_path var/"log/otto.log"
    error_log_path var/"log/otto.log"
    environment_variables OTTO_LAUNCHD_LABEL: "homebrew.mxcl.otto"
  end

  def caveats
    <<~EOS
      Otto uses YOUR Claude. Sign in once:   claude    (or set ANTHROPIC_API_KEY)

      First run builds a Python env and downloads the Whisper model (a few minutes):
        otto setup

      Then run it in the foreground:
        otto start                  # dashboard at http://127.0.0.1:8000
      or keep it in your menu bar, starting at login:
        brew services start otto

      Want it in Spotlight (⌘-Space) and double-clickable?
        otto install-app            # drops Otto.app into ~/Applications

      Custom wake word:   otto enroll-wake "hey otto"
      Uninstall the login item:   brew services stop otto
    EOS
  end

  test do
    assert_match "Otto", shell_output("#{bin}/otto help")
  end
end
