# Homebrew formula. Publish in a tap repo named `homebrew-tap`, then:
#     brew install nicolasdao/tap/autoshape
class Autoshape < Formula
  desc "Keeps a tethered hotspot usable while something is uploading"
  homepage "https://github.com/nicolasdao/autoshape"
  url "https://github.com/nicolasdao/autoshape/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "REPLACE_WITH_TARBALL_SHA256"
  license "BSD-3-Clause"
  depends_on :macos

  def install
    libexec.install "autoshape.py", "control.py", "tcpsense.py"
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
    EOS
  end

  test do
    assert_match "adaptive", shell_output("#{bin}/autoshape --help")
  end
end
