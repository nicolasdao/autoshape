# Homebrew formula for autoshape.
#
# This file is the SOURCE OF TRUTH. It is copied into the tap repo
# (nicolasdao/homebrew-tap, Formula/autoshape.rb) automatically by
# .github/workflows/release.yml whenever a vX.Y.Z tag is pushed, which also
# rewrites `url` and `sha256` for that tag. Do not hand-edit the copy in the
# tap - it will be overwritten on the next release.
#
# Users install with:
#     brew install nicolasdao/tap/autoshape
class Autoshape < Formula
  desc "Keeps a tethered hotspot usable while something is uploading"
  homepage "https://github.com/nicolasdao/autoshape"
  url "https://github.com/nicolasdao/autoshape/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "81aad187a033cb7bce4d95cc0a22feb392cc0d7150ca2649765ba73fc1039390"
  license "BSD-3-Clause"
  depends_on :macos

  def install
    # VERSION must ship alongside autoshape.py: version() reads it from its own
    # directory, and without it `autoshape --version` prints "unknown" and the
    # daily update check has nothing to compare against.
    libexec.install "autoshape.py", "control.py", "tcpsense.py", "VERSION"
    (bin/"autoshape").write <<~SH
      #!/bin/sh
      exec python3 "#{libexec}/autoshape.py" "$@"
    SH
    chmod 0755, bin/"autoshape"
  end

  def caveats
    <<~EOS
      autoshape changes packet scheduling, so it needs root:
          sudo autoshape
      Stop it with Ctrl-C. To clear anything left behind:
          sudo autoshape --off

      This copy is managed by Homebrew, so update it with:
          brew upgrade autoshape
      (`sudo autoshape --update` is for the curl install and will refuse here.)
    EOS
  end

  test do
    assert_match "adaptive", shell_output("#{bin}/autoshape --help")
    # Guards the bug where VERSION was not installed: this asserts the real
    # number reaches the binary rather than the "unknown" fallback.
    assert_match version.to_s, shell_output("#{bin}/autoshape --version")
  end
end
