{
  description = "Nevarro LLC financials";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      perSystem =
        { pkgs, ... }:
        {
          devShells.default = pkgs.mkShell {
            packages = with pkgs; [
              hledger
              hledger-fmt
              hledger-web
              pre-commit
              python3
            ];

            # Assumes `nix develop` is run from the repo root.
            shellHook = ''
              export LEDGER_FILE="$PWD/ledger/all.journal"
            '';
          };
        };
    };
}
