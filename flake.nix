{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    systems.url = "github:nix-systems/default";

    # Keep completions and type information aligned with the Picard API this
    # plugin targets. Update this input when moving to a newer Picard beta.
    picard = {
      url = "github:metabrainz/picard/release-3.0.0b7";
      flake = false;
    };
  };

  outputs =
    {
      nixpkgs,
      systems,
      picard,
      ...
    }:
    let
      forEachSystem = nixpkgs.lib.genAttrs (import systems);
    in
    {
      devShells = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # Expose the pinned Picard package as a normal site-package without
          # building the entire desktop application.
          picardSource = pkgs.python313.pkgs.toPythonModule (
            pkgs.runCommand "picard-3.0.0b7-source" { } ''
              mkdir -p "$out/${pkgs.python313.sitePackages}"
              ln -s "${picard}/picard" "$out/${pkgs.python313.sitePackages}/picard"
            ''
          );

          # nixpkgs' PyQt6 build does not include the upstream type stubs.
          # Supply them separately so Pyright can understand QtCore, QtWidgets,
          # signals, slots, and widget methods.
          pyqt6Stubs = pkgs.python313.pkgs.buildPythonPackage rec {
            pname = "PyQt6-stubs";
            version = "20250824";
            format = "wheel";
            src = pkgs.fetchPypi {
              pname = "pyqt6_stubs";
              inherit version format;
              dist = "py3";
              python = "py3";
              hash = "sha256-S3fcQPXat8pLqJn/9MHbjenMc3me6Y9WyuYzSfGKblI=";
            };
            doCheck = false;
          };

          python = pkgs.python313.withPackages (
            pythonPackages: with pythonPackages; [
              charset-normalizer
              discid
              markdown
              mutagen
              pygit2
              pyjwt
              pyqt6
              pyqt6Stubs
              pyyaml
              tomli
              tomlkit
              picardSource
              pytest
            ]
          );

          check = pkgs.writeShellApplication {
            name = "check";
            runtimeInputs = [
              pkgs.pyright
              pkgs.ruff
            ];
            text = ''
              ruff check .
              pyright
            '';
          };
        in
        {
          default = pkgs.mkShellNoCC {
            name = "simple-classical";
            packages = [
              python
              pkgs.pyright
              pkgs.ruff
              check
            ];
          };
        }
      );
    };
}
