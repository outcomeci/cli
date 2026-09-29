class OutcomeciCli < Formula
  include Language::Python::Virtualenv

  desc "Write, run and publish OutcomeCI workflows"
  homepage "https://github.com/outcomeci/cli"
  url "https://files.pythonhosted.org/packages/source/o/outcomeci-cli/outcomeci_cli-0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "oci", shell_output("#{bin}/oci --help")
    assert_match version.to_s, shell_output("#{bin}/oci --version")
  end
end
