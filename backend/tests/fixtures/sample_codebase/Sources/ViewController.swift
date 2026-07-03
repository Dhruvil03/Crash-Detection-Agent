import UIKit

class ViewController: UIViewController {
    var data: [String]?

    override func viewDidLoad() {
        super.viewDidLoad()
        loadData()
    }

    func loadData() {
        let first = data!.first  // crashes if data is still nil
        print(first ?? "none")
    }

    func fetchFromNetwork() {
        NetworkClient.shared.fetch { [weak self] result in
            self?.data = result  // set asynchronously, after viewDidLoad may have already run
        }
    }
}
