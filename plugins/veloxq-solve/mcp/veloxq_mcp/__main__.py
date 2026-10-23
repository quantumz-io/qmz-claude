import sys

if sys.argv[1:2] == ["serve"]:
    from .hosted import main

    main(sys.argv[2:])
else:
    from .server import main

    main()
